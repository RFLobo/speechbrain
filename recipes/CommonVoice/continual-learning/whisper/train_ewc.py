#!/usr/bin/env python3

"""Recipe for fine-tuning a Whisper-based ASR system on Common Voice in a continual
learning fashion via Elastic Weight Consolidation (https://arxiv.org/abs/1612.00796).

The following technical tricks were implemented to improve performance:
- use custom decoding implementation (faster than built-in searchers
  and supports decoding with predicted batch of languages)
- apply the correct padding tokens directly in the dataloader
- use cross-entropy loss (with `ignore_index` correctly set) instead of log softmax + NLL
- remove unnecessary `undo_padding` since padding tokens are now set correctly
- improve memory usage during model recovery (see https://github.com/speechbrain/speechbrain/pull/1743)
- optionally use gradient checkpointing

To run this recipe, do the following:
> python train_ewc.py hparams/train_ewc.yaml

NOTE: although checkpoints are saved regularly, automatic experiment resumption is not supported.
      To manually resume an experiment, you have to modify the script to load the correct checkpoint
      and set the corresponding state variables (e.g. current locale).

Authors
 * Luca Della Libera 2023
 * Pooneh Mousavi 2023
"""

import logging
import os
import pathlib
import sys
import time

import ptflops
import torch
import torchaudio
import torchinfo
from hyperpyyaml import load_hyperpyyaml

import speechbrain as sb
from speechbrain.dataio.batch import PaddedBatch
from speechbrain.tokenizers.SentencePiece import SentencePiece
from speechbrain.utils.distributed import run_on_main

from common_voice_prepare import prepare_common_voice


class ASR(sb.Brain):
    def compute_forward(self, batch, stage):
        """Forward computations from the waveform batches to the output probabilities."""
        batch = batch.to(self.device)
        wavs, wav_lens = batch.sig
        bos_tokens, _ = batch.tokens_bos

        # Forward encoder + decoder
        if self.hparams.gradient_checkpointing:
            bos_tokens.requires_grad_()
            enc_out, logits, _ = torch.utils.checkpoint.checkpoint(
                self.modules.whisper, wavs, bos_tokens,
            )
        else:
            enc_out, logits, _ = self.modules.whisper(wavs, bos_tokens)

        hyps = None
        if stage != sb.Stage.TRAIN:
            hyps, _ = self.modules.whisper.generate(
                audio_features=enc_out,
                forced_decoder_locale=self.hparams.forced_decoder_locale,
                max_gen_tokens=self.hparams.max_gen_tokens,
            )

        return logits, hyps

    def compute_objectives(self, predictions, batch, stage):
        """Computes the loss given predictions and targets."""
        logits, hyps = predictions
        ids = batch.id
        tokens_eos, _ = batch.tokens_eos

        loss = self.hparams.ce_loss(
            logits.flatten(end_dim=-2), tokens_eos.flatten()
        )

        if stage == sb.Stage.TRAIN and hasattr(self.hparams, "all_ewc_params"):
            for name, param in self.modules.whisper.named_parameters():
                if not param.requires_grad:
                    continue
                for (old_param, fisher) in self.hparams.all_ewc_params:
                    if "embed_tokens.weight" in name:
                        diff = param.shape[0] - old_param[name].shape[0]
                        old_param[name] = torch.nn.functional.pad(
                            old_param[name], [0, 0, 0, diff]
                        )
                        fisher[name] = torch.nn.functional.pad(
                            fisher[name], [0, 0, 0, diff]
                        )
                    loss += (
                        (
                            fisher[name]
                            * (old_param[name] - param.to(fisher[name].device))
                            ** 2
                        )
                        .sum()
                        .to(self.device)
                        * 0.5
                        * self.hparams.ewc_lambda
                    )

        if stage != sb.Stage.TRAIN:
            target_words = batch.target_wrd

            # Decode predicted tokens to words
            predicted_words = self.tokenizer.batch_decode(
                hyps, skip_special_tokens=True
            )

            if self.hparams.normalize_transcripts:
                predicted_words = [
                    self.tokenizer._normalize(text).split(" ")
                    for text in predicted_words
                ]
            else:
                predicted_words = [text.split(" ") for text in predicted_words]

            self.wer_metric.append(ids, predicted_words, target_words)
            self.cer_metric.append(ids, predicted_words, target_words)

        return loss

    def on_stage_start(self, stage, epoch=None):
        """Gets called at the beginning of each epoch"""
        if stage != sb.Stage.TRAIN:
            self.cer_metric = self.hparams.cer_computer()
            self.wer_metric = self.hparams.wer_computer()

    def on_stage_end(self, stage, stage_loss, epoch=None):
        """Gets called at the end of an epoch."""
        # Compute/store important stats
        stage_stats = {"loss": stage_loss}
        if stage == sb.Stage.TRAIN:
            self.train_stats = stage_stats
        else:
            stage_stats["CER"] = self.cer_metric.summarize("error_rate")
            stage_stats["WER"] = self.wer_metric.summarize("error_rate")

        # Perform end-of-iteration things, like annealing, logging, etc.
        if stage == sb.Stage.VALID:
            old_lr, new_lr = self.hparams.lr_annealing(stage_stats["loss"])
            sb.nnet.schedulers.update_learning_rate(self.optimizer, new_lr)
            stats_meta_data = {
                "epoch": epoch,
                "lr": old_lr,
            }
            self.hparams.train_logger.log_stats(
                stats_meta=stats_meta_data,
                train_stats=self.train_stats,
                valid_stats=stage_stats,
            )
            self.checkpointer.save_and_keep_only(
                meta={"WER": stage_stats["WER"]}, min_keys=["WER"],
            )
        elif stage == sb.Stage.TEST:
            self.hparams.train_logger.log_stats(
                stats_meta={"Epoch loaded": self.hparams.epoch_counter.current},
                test_stats=stage_stats,
            )
            with open(self.hparams.wer_file, "w", encoding="utf-8") as w:
                self.wer_metric.write_stats(w)


class EWCParamsComputer(ASR):
    def zero_grad(self, set_to_none=False):
        pass

    def fit_batch(self, batch):
        outputs = self.compute_forward(batch, sb.Stage.TRAIN)
        loss = self.compute_objectives(outputs, batch, sb.Stage.TRAIN)
        with self.no_sync(False):
            loss.backward()
        self.on_fit_batch_end(batch, outputs, loss, True)
        return loss.detach().cpu()


def dataio_prepare(hparams, tokenizer):
    """This function prepares the datasets to be used in the brain class.
    It also defines the data processing pipeline through user-defined functions."""
    train_data = sb.dataio.dataset.DynamicItemDataset.from_csv(
        csv_path=os.path.join(hparams["data_dir"], "train.csv"),
        replacements={"data_root": hparams["data_dir"]},
    )

    if hparams["sorting"] in ["descending", "ascending"]:
        # We sort training data to speed up training and get better results
        train_data = train_data.filtered_sorted(
            sort_key="duration",
            reverse=hparams["sorting"] == "descending",
            key_max_value={"duration": hparams["avoid_if_longer_than"]},
        )
        # When sorting do not shuffle in dataloader otherwise it is pointless
        hparams["train_dataloader_kwargs"]["shuffle"] = False

    elif hparams["sorting"] != "random":
        raise ValueError(
            f"`sorting` ({hparams['sorting']}) must be random, ascending or descending"
        )

    # reverse=True to fail fast in case of out-of-memory error
    valid_data = sb.dataio.dataset.DynamicItemDataset.from_csv(
        csv_path=os.path.join(hparams["data_dir"], "dev.csv"),
        replacements={"data_root": hparams["data_dir"]},
    ).filtered_sorted(
        sort_key="duration",
        reverse=True,
        key_max_value={"duration": hparams["avoid_if_longer_than"]},
    )

    test_data = sb.dataio.dataset.DynamicItemDataset.from_csv(
        csv_path=os.path.join(hparams["data_dir"], "test.csv"),
        replacements={"data_root": hparams["data_dir"]},
    ).filtered_sorted(
        sort_key="duration",
        reverse=True,
        key_max_value={"duration": hparams["avoid_if_longer_than"]},
    )

    datasets = [train_data, valid_data, test_data]

    # 2. Define audio pipeline:
    @sb.utils.data_pipeline.takes("mp3")
    @sb.utils.data_pipeline.provides("sig")
    def audio_pipeline(mp3):
        info = torchaudio.info(mp3)
        sig = sb.dataio.dataio.read_audio(mp3)
        resampled = torchaudio.transforms.Resample(
            info.sample_rate, hparams["sample_rate"],
        )(sig)
        return resampled

    sb.dataio.dataset.add_dynamic_item(datasets, audio_pipeline)

    # 3. Define text pipeline:
    @sb.utils.data_pipeline.takes("wrd", "locale")
    @sb.utils.data_pipeline.provides("tokens_bos", "tokens_eos", "target_wrd")
    def text_pipeline(wrd, locale):
        language = tokenizer.supported_languages.get(
            locale, "english"
        )  # Use English if unknown
        tokenizer.set_prefix_tokens(language=language)
        tokens_list = tokenizer.encode(wrd)
        # Remove BOS and EOS tokens from tokens_list
        bos_index, tokens_list, eos_index = (
            tokens_list[0],
            tokens_list[1:-1],
            tokens_list[-1],
        )
        tokens_list = tokens_list[: hparams["max_target_length"] - 1]
        tokens_bos = torch.LongTensor([bos_index] + tokens_list)
        yield tokens_bos
        tokens_eos = torch.LongTensor(tokens_list + [eos_index])
        yield tokens_eos
        if hparams["normalize_transcripts"]:
            wrd = tokenizer._normalize(wrd)
        wrd = wrd.split(" ")
        # When `ref_tokens` is an empty string add dummy space
        # to avoid division by 0 when computing WER/CER
        for i, char in enumerate(wrd):
            if len(char) == 0:
                wrd[i] = " "
        yield wrd

    sb.dataio.dataset.add_dynamic_item(datasets, text_pipeline)

    # 4. Set output:
    sb.dataio.dataset.set_output_keys(
        datasets, ["id", "sig", "tokens_bos", "tokens_eos", "target_wrd"],
    )

    return train_data, valid_data, test_data


def compute_ewc_params(hparams, run_opts, locales):
    tokenizer = hparams["whisper"].tokenizer
    max_grad_norm = hparams.get("max_grad_norm", 5.0)
    grad_accumulation_factor = hparams.get("grad_accumulation_factor", 1)
    auto_mix_prec = hparams.get("auto_mix_prec", False)
    hparams["max_grad_norm"] = float("inf")
    hparams["auto_mix_prec"] = False
    hparams["grad_accumulation_factor"] = 1

    # Multi-gpu (ddp) save data preparation
    run_on_main(
        prepare_common_voice,
        kwargs={
            "locales": locales,
            "data_dir": hparams["data_dir"],
            "max_durations": hparams["max_durations"],
        },
    )

    # Here we create the datasets objects as well as tokenization and encoding
    train_data, _, _ = dataio_prepare(hparams, tokenizer)

    # Trainer initialization
    asr_brain = EWCParamsComputer(
        modules=hparams["modules"], hparams=hparams, run_opts=run_opts,
    )

    # We dynamically add the tokenizer to our brain class
    # NB: This tokenizer corresponds to the one used for Whisper
    asr_brain.tokenizer = tokenizer

    # Training (no parameter update)
    asr_brain.fit(
        range(1),
        train_data,
        train_loader_kwargs=hparams["train_dataloader_kwargs"],
    )

    params, fisher = {}, {}
    with torch.no_grad():
        for name, param in hparams["whisper"].named_parameters():
            if not param.requires_grad:
                continue
            params[name] = param.clone().cpu()
            fisher[name] = (param.grad.clone() ** 2).cpu()
    hparams["whisper"].zero_grad(set_to_none=True)

    hparams["max_grad_norm"] = max_grad_norm
    hparams["auto_mix_prec"] = auto_mix_prec
    hparams["grad_accumulation_factor"] = grad_accumulation_factor

    return params, fisher


def test(hparams, run_opts, locales, wer_file="wer_test.txt"):
    # Test on old + new locales
    for locale in locales:
        # Multi-gpu (ddp) save data preparation
        run_on_main(
            prepare_common_voice,
            kwargs={
                "locales": [locale],
                "data_dir": hparams["data_dir"],
                "max_durations": hparams["max_durations"],
            },
        )

        if locale in ["zh-CN", "ja"]:
            # Use CER instead of WER (spaces are not used)
            hparams[
                "wer_computer"
            ] = lambda *args, **kwargs: sb.utils.metric_stats.ErrorRateStats(
                split_tokens=True
            )
        else:
            hparams["wer_computer"] = sb.utils.metric_stats.ErrorRateStats

        # Set forced decoder locale
        hparams["forced_decoder_locale"] = locale

        # Define tokenizer
        tokenizer = hparams["whisper"].tokenizer

        # Here we create the datasets objects as well as tokenization and encoding
        _, _, test_data = dataio_prepare(hparams, tokenizer)

        # Trainer initialization
        asr_brain = ASR(
            modules=hparams["modules"], hparams=hparams, run_opts=run_opts,
        )

        # We dynamically add the tokenizer to our brain class
        # NB: This tokenizer corresponds to the one used for Whisper
        asr_brain.tokenizer = tokenizer

        # Testing
        locale_dir = os.path.join(hparams["output_dir"], locale)
        os.makedirs(locale_dir, exist_ok=True)
        asr_brain.hparams.wer_file = os.path.join(locale_dir, wer_file)
        asr_brain.evaluate(
            test_data,
            min_key="WER",
            test_loader_kwargs=hparams["valid_dataloader_kwargs"],
        )

    # MACs not 100% accurate but still useful for comparisons
    profile(hparams)


def train(hparams, run_opts):
    test(
        hparams, run_opts, hparams["old_locales"], f"wer_test_before.txt",
    )

    # Train on new locales
    all_ewc_params = []
    for i, locale in enumerate(hparams["new_locales"]):
        # Remove old EWC parameters
        hparams.pop("all_ewc_params", None)

        # Compute new EWC parameters
        if i == 0:
            ewc_params = compute_ewc_params(
                hparams, run_opts, hparams["old_locales"]
            )
        else:
            ewc_params = compute_ewc_params(
                hparams, run_opts, [hparams["new_locales"][i - 1]]
            )

        all_ewc_params.append(ewc_params)

        hparams["all_ewc_params"] = all_ewc_params

        # Multi-gpu (ddp) save data preparation
        run_on_main(
            prepare_common_voice,
            kwargs={
                "locales": [locale],
                "data_dir": hparams["data_dir"],
                "max_durations": hparams["max_durations"],
            },
        )

        # Fit sentence-piece tokenizer on new language
        sp_dir = os.path.join(hparams["save_dir"], locale)
        os.makedirs(sp_dir, exist_ok=True)
        sp = SentencePiece(
            model_dir=sp_dir,
            vocab_size=-1,
            annotation_train=os.path.join(hparams["data_dir"], "train.csv"),
            annotation_read="wrd",
            model_type="char",
        )

        # Get sentence-piece tokenizer vocabulary
        vocab = [sp.sp.id_to_piece(id) for id in range(sp.sp.get_piece_size())]

        # Remove leading "▁" character
        vocab = [wrd[1:] if wrd.startswith("▁") else wrd for wrd in vocab]

        # Remove "<unk>" token
        new_tokens = vocab[1:]

        # Add new language token
        new_tokens = [f"<|{locale.lower()}|>"] + new_tokens
        tokenizer = hparams["whisper"].tokenizer
        tokenizer._additional_special_tokens += [f"<|{locale.lower()}|>"]
        tokenizer.supported_languages.update({locale.lower(): locale.lower()})
        tokenizer.to_language_codes.update({locale.lower(): locale.lower()})

        # Remove tokens that are already in Whisper tokenizer's vocabulary
        new_tokens = set(new_tokens) - set(tokenizer.get_vocab().keys())

        # Add the tokens to Whisper tokenizer's vocabulary
        tokenizer.add_tokens(list(new_tokens))

        # Log total number of tokens
        logging.info(
            f"Total number of tokens: {hparams['whisper'].model.decoder.embed_tokens.num_embeddings}"
        )

        # Add new random embeddings to Whisper for the new tokens
        hparams["whisper"].model.resize_token_embeddings(len(tokenizer))

        # Log total number of tokens
        logging.info(
            f"Total number of tokens: {hparams['whisper'].model.decoder.embed_tokens.num_embeddings}"
        )

        # Set forced decoder locale
        hparams["forced_decoder_locale"] = locale

        # Here we create the datasets objects as well as tokenization and encoding
        train_data, valid_data, test_data = dataio_prepare(hparams, tokenizer)

        # Trainer initialization
        checkpoint_dir = os.path.join(hparams["save_dir"], locale)
        os.makedirs(checkpoint_dir, exist_ok=True)
        hparams["checkpointer"].checkpoints_dir = pathlib.Path(checkpoint_dir)
        hparams["lr_annealing"].hyperparam_value = hparams["lr"]
        hparams["lr_annealing"].metric_values.clear()
        hparams["lr_annealing"].current_patient = 0
        asr_brain = ASR(
            modules=hparams["modules"],
            hparams=hparams,
            run_opts=run_opts,
            opt_class=hparams["opt_class"],
            checkpointer=hparams["checkpointer"],
        )

        # We dynamically add the tokenizer to our brain class
        # NB: This tokenizer corresponds to the one used for Whisper
        asr_brain.tokenizer = tokenizer

        # Training
        hparams["valid_dataloader_kwargs"].pop("ckpt_prefix", None)
        hparams["epoch_counter"].current = 0
        asr_brain.fit(
            hparams["epoch_counter"],
            train_data,
            valid_data,
            train_loader_kwargs=hparams["train_dataloader_kwargs"],
            valid_loader_kwargs=hparams["valid_dataloader_kwargs"],
        )

        # Testing
        test(
            hparams,
            run_opts,
            hparams["old_locales"] + hparams["new_locales"][: i + 1],
            f"wer_test_after_{locale}.txt",
        )


def profile(hparams):
    class Model(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.whisper = hparams["whisper"]
            self.wavs = torch.randn(
                1, hparams["sample_rate"], device=run_opts["device"],
            )
            self.bos_tokens = torch.ones(
                1,
                self.whisper.model.config.max_length,
                dtype=torch.int,
                device=run_opts["device"],
            )

        @torch.no_grad()
        def forward(self, _=None):
            enc_out, logits, _ = self.whisper(self.wavs, self.bos_tokens)
            return logits

    model = Model().eval().to(run_opts["device"])

    macs, params = ptflops.get_model_complexity_info(
        model, (1,), as_strings=True,
    )
    time_start = time.time()
    model()
    torch.cuda.synchronize()
    time_stop = time.time() - time_start
    max_mem = torch.cuda.max_memory_allocated("cuda") / 10 ** 9
    result = {
        "MACs": macs,
        "memory": max_mem,
        "time": time_stop,
    }
    logging.info(torchinfo.summary(model, verbose=0))
    logging.info(result)


if __name__ == "__main__":
    # Command-line interface
    hparams_file, run_opts, overrides = sb.parse_arguments(sys.argv[1:])

    # If distributed_launch=True then
    # create ddp_group with the right communication protocol
    sb.utils.distributed.ddp_init_group(run_opts)

    with open(hparams_file) as fin:
        hparams = load_hyperpyyaml(fin, overrides)

    # Create experiment directory
    sb.create_experiment_directory(
        experiment_directory=hparams["output_dir"],
        hyperparams_to_save=hparams_file,
        overrides=overrides,
    )

    class CustomPaddedBatch(PaddedBatch):
        def __init__(self, examples, *args, **kwargs):
            for k in ["tokens_bos", "tokens_eos"]:
                max_len = max([len(x[k]) for x in examples])
                pad_value = 0.0
                if k in ["tokens_bos"]:
                    pad_value = hparams["whisper"].tokenizer.pad_token_id
                elif k == "tokens_eos":
                    pad_value = hparams["ignore_index"]
                for example in examples:
                    x = example[k]
                    example[k] = torch.nn.functional.pad(
                        x, [0, max_len - len(x)], value=pad_value
                    )
            super().__init__(examples, *args, **kwargs)

    hparams["train_dataloader_kwargs"]["collate_fn"] = CustomPaddedBatch
    hparams["valid_dataloader_kwargs"]["collate_fn"] = CustomPaddedBatch

    # Train
    start_time = time.time()
    train(hparams, run_opts)
    duration = time.time() - start_time
    logging.info(f"Time elapsed: {duration} seconds")
