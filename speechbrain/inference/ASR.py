""" Specifies the inference interfaces for Automatic speech Recognition (ASR) modules.

Authors:
 * Aku Rouhe 2021
 * Peter Plantinga 2021
 * Loren Lugosch 2020
 * Mirco Ravanelli 2020
 * Titouan Parcollet 2021
 * Abdel Heba 2021
 * Andreas Nautsch 2022, 2023
 * Pooneh Mousavi 2023
 * Sylvain de Langen 2023
 * Adel Moumen 2023, 2024
 * Pradnya Kandarkar 2023
"""
from dataclasses import dataclass
from typing import Any, Optional
import torch
import sentencepiece
import speechbrain
from speechbrain.inference.interfaces import Pretrained
import functools
from speechbrain.utils.fetching import fetch
from speechbrain.utils.data_utils import split_path
from speechbrain.utils.dynamic_chunk_training import DynChunkTrainConfig
from speechbrain.utils.streaming import split_fixed_chunks


class EncoderDecoderASR(Pretrained):
    """A ready-to-use Encoder-Decoder ASR model

    The class can be used either to run only the encoder (encode()) to extract
    features or to run the entire encoder-decoder model
    (transcribe()) to transcribe speech. The given YAML must contain the fields
    specified in the *_NEEDED[] lists.

    Example
    -------
    >>> from speechbrain.inference.ASR import EncoderDecoderASR
    >>> tmpdir = getfixture("tmpdir")
    >>> asr_model = EncoderDecoderASR.from_hparams(
    ...     source="speechbrain/asr-crdnn-rnnlm-librispeech",
    ...     savedir=tmpdir,
    ... )  # doctest: +SKIP
    >>> asr_model.transcribe_file("tests/samples/single-mic/example2.flac")  # doctest: +SKIP
    "MY FATHER HAS REVEALED THE CULPRIT'S NAME"
    """

    HPARAMS_NEEDED = ["tokenizer"]
    MODULES_NEEDED = ["encoder", "decoder"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tokenizer = self.hparams.tokenizer
        self.transducer_beam_search = False
        self.transformer_beam_search = False
        if hasattr(self.hparams, "transducer_beam_search"):
            self.transducer_beam_search = self.hparams.transducer_beam_search
        if hasattr(self.hparams, "transformer_beam_search"):
            self.transformer_beam_search = self.hparams.transformer_beam_search

    def transcribe_file(self, path, **kwargs):
        """Transcribes the given audiofile into a sequence of words.

        Arguments
        ---------
        path : str
            Path to audio file which to transcribe.

        Returns
        -------
        str
            The audiofile transcription produced by this ASR system.
        """
        waveform = self.load_audio(path, **kwargs)
        # Fake a batch:
        batch = waveform.unsqueeze(0)
        rel_length = torch.tensor([1.0])
        predicted_words, predicted_tokens = self.transcribe_batch(
            batch, rel_length
        )
        return predicted_words[0]

    def encode_batch(self, wavs, wav_lens):
        """Encodes the input audio into a sequence of hidden states

        The waveforms should already be in the model's desired format.
        You can call:
        ``normalized = EncoderDecoderASR.normalizer(signal, sample_rate)``
        to get a correctly converted signal in most cases.

        Arguments
        ---------
        wavs : torch.Tensor
            Batch of waveforms [batch, time, channels] or [batch, time]
            depending on the model.
        wav_lens : torch.Tensor
            Lengths of the waveforms relative to the longest one in the
            batch, tensor of shape [batch]. The longest one should have
            relative length 1.0 and others len(waveform) / max_length.
            Used for ignoring padding.

        Returns
        -------
        torch.Tensor
            The encoded batch
        """
        wavs = wavs.float()
        wavs, wav_lens = wavs.to(self.device), wav_lens.to(self.device)
        encoder_out = self.mods.encoder(wavs, wav_lens)
        if self.transformer_beam_search:
            encoder_out = self.mods.transformer.encode(encoder_out, wav_lens)
        return encoder_out

    def transcribe_batch(self, wavs, wav_lens):
        """Transcribes the input audio into a sequence of words

        The waveforms should already be in the model's desired format.
        You can call:
        ``normalized = EncoderDecoderASR.normalizer(signal, sample_rate)``
        to get a correctly converted signal in most cases.

        Arguments
        ---------
        wavs : torch.Tensor
            Batch of waveforms [batch, time, channels] or [batch, time]
            depending on the model.
        wav_lens : torch.Tensor
            Lengths of the waveforms relative to the longest one in the
            batch, tensor of shape [batch]. The longest one should have
            relative length 1.0 and others len(waveform) / max_length.
            Used for ignoring padding.

        Returns
        -------
        list
            Each waveform in the batch transcribed.
        tensor
            Each predicted token id.
        """
        with torch.no_grad():
            wav_lens = wav_lens.to(self.device)
            encoder_out = self.encode_batch(wavs, wav_lens)
            if self.transducer_beam_search:
                inputs = [encoder_out]
            else:
                inputs = [encoder_out, wav_lens]
            predicted_tokens, _, _, _ = self.mods.decoder(*inputs)
            predicted_words = [
                self.tokenizer.decode_ids(token_seq)
                for token_seq in predicted_tokens
            ]
        return predicted_words, predicted_tokens

    def forward(self, wavs, wav_lens):
        """Runs full transcription - note: no gradients through decoding"""
        return self.transcribe_batch(wavs, wav_lens)


class EncoderASR(Pretrained):
    """A ready-to-use Encoder ASR model

    The class can be used either to run only the encoder (encode()) to extract
    features or to run the entire encoder + decoder function model
    (transcribe()) to transcribe speech. The given YAML must contain the fields
    specified in the *_NEEDED[] lists.

    Example
    -------
    >>> from speechbrain.inference.ASR import EncoderASR
    >>> tmpdir = getfixture("tmpdir")
    >>> asr_model = EncoderASR.from_hparams(
    ...     source="speechbrain/asr-wav2vec2-commonvoice-fr",
    ...     savedir=tmpdir,
    ... ) # doctest: +SKIP
    >>> asr_model.transcribe_file("samples/audio_samples/example_fr.wav") # doctest: +SKIP
    """

    HPARAMS_NEEDED = ["tokenizer", "decoding_function"]
    MODULES_NEEDED = ["encoder"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.tokenizer = self.hparams.tokenizer
        self.set_decoding_function()

    def set_decoding_function(self):
        """Set the decoding function based on the parameters defined in the hyperparameter file.

        The decoding function is determined by the `decoding_function` specified in the hyperparameter file.
        It can be either a functools.partial object representing a decoding function or an instance of
        `speechbrain.decoders.ctc.CTCBaseSearcher` for beam search decoding.

        Raises:
            ValueError: If the decoding function is neither a functools.partial nor an instance of
                        speechbrain.decoders.ctc.CTCBaseSearcher.

        Note:
            - For greedy decoding (functools.partial), the provided `decoding_function` is assigned directly.
            - For CTCBeamSearcher decoding, an instance of the specified `decoding_function` is created, and
            additional parameters are added based on the tokenizer type.
        """
        # Greedy Decoding case
        if isinstance(self.hparams.decoding_function, functools.partial):
            self.decoding_function = self.hparams.decoding_function
        # CTCBeamSearcher case
        else:
            # 1. check if the decoding function is an instance of speechbrain.decoders.CTCBaseSearcher
            if issubclass(
                self.hparams.decoding_function,
                speechbrain.decoders.ctc.CTCBaseSearcher,
            ):
                # If so, we need to retrieve the vocab list from the tokenizer.
                # We also need to check if the tokenizer is a sentencepiece or a CTCTextEncoder.
                if isinstance(
                    self.tokenizer, speechbrain.dataio.encoder.CTCTextEncoder
                ):
                    ind2lab = self.tokenizer.ind2lab
                    vocab_list = [ind2lab[x] for x in range(len(ind2lab))]
                elif isinstance(
                    self.tokenizer, sentencepiece.SentencePieceProcessor
                ):
                    vocab_list = [
                        self.tokenizer.id_to_piece(i)
                        for i in range(self.tokenizer.vocab_size())
                    ]
                else:
                    raise ValueError(
                        "The tokenizer must be sentencepiece or CTCTextEncoder"
                    )

                # We can now instantiate the decoding class and add all the parameters
                if hasattr(self.hparams, "test_beam_search"):
                    opt_beam_search_params = self.hparams.test_beam_search
                    # check if the kenlm_model_path is provided and fetch it if necessary
                    if "kenlm_model_path" in opt_beam_search_params:
                        source, fl = split_path(
                            opt_beam_search_params["kenlm_model_path"]
                        )
                        kenlm_model_path = str(
                            fetch(fl, source=source, savedir=".")
                        )
                        # we need to update the kenlm_model_path in the opt_beam_search_params
                        opt_beam_search_params[
                            "kenlm_model_path"
                        ] = kenlm_model_path
                else:
                    opt_beam_search_params = {}
                self.decoding_function = self.hparams.decoding_function(
                    **opt_beam_search_params, vocab_list=vocab_list
                )
            else:
                raise ValueError(
                    "The decoding function must be an instance of speechbrain.decoders.CTCBaseSearcher"
                )

    def transcribe_file(self, path, **kwargs):
        """Transcribes the given audiofile into a sequence of words.

        Arguments
        ---------
        path : str
            Path to audio file which to transcribe.

        Returns
        -------
        str
            The audiofile transcription produced by this ASR system.
        """
        waveform = self.load_audio(path, **kwargs)
        # Fake a batch:
        batch = waveform.unsqueeze(0)
        rel_length = torch.tensor([1.0])
        predicted_words, predicted_tokens = self.transcribe_batch(
            batch, rel_length
        )
        return str(predicted_words[0])

    def encode_batch(self, wavs, wav_lens):
        """Encodes the input audio into a sequence of hidden states

        The waveforms should already be in the model's desired format.
        You can call:
        ``normalized = EncoderASR.normalizer(signal, sample_rate)``
        to get a correctly converted signal in most cases.

        Arguments
        ---------
        wavs : torch.Tensor
            Batch of waveforms [batch, time, channels] or [batch, time]
            depending on the model.
        wav_lens : torch.Tensor
            Lengths of the waveforms relative to the longest one in the
            batch, tensor of shape [batch]. The longest one should have
            relative length 1.0 and others len(waveform) / max_length.
            Used for ignoring padding.

        Returns
        -------
        torch.Tensor
            The encoded batch
        """
        wavs = wavs.float()
        wavs, wav_lens = wavs.to(self.device), wav_lens.to(self.device)
        encoder_out = self.mods.encoder(wavs, wav_lens)
        return encoder_out

    def transcribe_batch(self, wavs, wav_lens):
        """Transcribes the input audio into a sequence of words

        The waveforms should already be in the model's desired format.
        You can call:
        ``normalized = EncoderASR.normalizer(signal, sample_rate)``
        to get a correctly converted signal in most cases.

        Arguments
        ---------
        wavs : torch.Tensor
            Batch of waveforms [batch, time, channels] or [batch, time]
            depending on the model.
        wav_lens : torch.Tensor
            Lengths of the waveforms relative to the longest one in the
            batch, tensor of shape [batch]. The longest one should have
            relative length 1.0 and others len(waveform) / max_length.
            Used for ignoring padding.

        Returns
        -------
        list
            Each waveform in the batch transcribed.
        tensor
            Each predicted token id.
        """
        with torch.no_grad():
            wav_lens = wav_lens.to(self.device)
            encoder_out = self.encode_batch(wavs, wav_lens)
            predictions = self.decoding_function(encoder_out, wav_lens)
            is_ctc_text_encoder_tokenizer = isinstance(
                self.tokenizer, speechbrain.dataio.encoder.CTCTextEncoder
            )
            if isinstance(self.hparams.decoding_function, functools.partial):
                if is_ctc_text_encoder_tokenizer:
                    predicted_words = [
                        "".join(self.tokenizer.decode_ndim(token_seq))
                        for token_seq in predictions
                    ]
                else:
                    predicted_words = [
                        self.tokenizer.decode_ids(token_seq)
                        for token_seq in predictions
                    ]
            else:
                predicted_words = [hyp[0].text for hyp in predictions]

        return predicted_words, predictions

    def forward(self, wavs, wav_lens):
        """Runs the encoder"""
        return self.encode_batch(wavs, wav_lens)


class WhisperASR(Pretrained):
    """A ready-to-use Whisper ASR model

    The class can be used  to  run the entire encoder-decoder whisper model
    (transcribe()) to transcribe speech. The given YAML must contains the fields
    specified in the *_NEEDED[] lists.

    Example
    -------
    >>> from speechbrain.inference.ASR import WhisperASR
    >>> tmpdir = getfixture("tmpdir")
    >>> asr_model = WhisperASR.from_hparams(source="speechbrain/asr-whisper-medium-commonvoice-it", savedir=tmpdir,) # doctest: +SKIP
    >>> asr_model.transcribe_file("speechbrain/asr-whisper-medium-commonvoice-it/example-it.wav")  # doctest: +SKIP
    """

    HPARAMS_NEEDED = ["language"]
    MODULES_NEEDED = ["whisper", "decoder"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tokenizer = self.hparams.whisper.tokenizer
        self.tokenizer.set_prefix_tokens(
            self.hparams.language, "transcribe", False
        )
        self.hparams.decoder.set_decoder_input_tokens(
            self.tokenizer.prefix_tokens
        )

    def transcribe_file(self, path):
        """Transcribes the given audiofile into a sequence of words.

        Arguments
        ---------
        path : str
            Path to audio file which to transcribe.

        Returns
        -------
        str
            The audiofile transcription produced by this ASR system.
        """
        waveform = self.load_audio(path)
        # Fake a batch:
        batch = waveform.unsqueeze(0)
        rel_length = torch.tensor([1.0])
        predicted_words, predicted_tokens = self.transcribe_batch(
            batch, rel_length
        )
        return " ".join(predicted_words[0])

    def encode_batch(self, wavs, wav_lens):
        """Encodes the input audio into a sequence of hidden states

        The waveforms should already be in the model's desired format.
        You can call:
        ``normalized = EncoderDecoderASR.normalizer(signal, sample_rate)``
        to get a correctly converted signal in most cases.

        Arguments
        ---------
        wavs : torch.tensor
            Batch of waveforms [batch, time, channels].
        wav_lens : torch.tensor
            Lengths of the waveforms relative to the longest one in the
            batch, tensor of shape [batch]. The longest one should have
            relative length 1.0 and others len(waveform) / max_length.
            Used for ignoring padding.

        Returns
        -------
        torch.tensor
            The encoded batch
        """
        wavs = wavs.float()
        wavs, wav_lens = wavs.to(self.device), wav_lens.to(self.device)
        encoder_out = self.mods.whisper.forward_encoder(wavs)
        return encoder_out

    def transcribe_batch(self, wavs, wav_lens):
        """Transcribes the input audio into a sequence of words

        The waveforms should already be in the model's desired format.
        You can call:
        ``normalized = EncoderDecoderASR.normalizer(signal, sample_rate)``
        to get a correctly converted signal in most cases.

        Arguments
        ---------
        wavs : torch.tensor
            Batch of waveforms [batch, time, channels].
        wav_lens : torch.tensor
            Lengths of the waveforms relative to the longest one in the
            batch, tensor of shape [batch]. The longest one should have
            relative length 1.0 and others len(waveform) / max_length.
            Used for ignoring padding.

        Returns
        -------
        list
            Each waveform in the batch transcribed.
        tensor
            Each predicted token id.
        """
        with torch.no_grad():
            wav_lens = wav_lens.to(self.device)
            encoder_out = self.encode_batch(wavs, wav_lens)
            predicted_tokens, _, _, _ = self.mods.decoder(encoder_out, wav_lens)
            predicted_words = self.tokenizer.batch_decode(
                predicted_tokens, skip_special_tokens=True
            )
            if self.hparams.normalized_transcripts:
                predicted_words = [
                    self.tokenizer._normalize(text).split(" ")
                    for text in predicted_words
                ]

        return predicted_words, predicted_tokens

    def forward(self, wavs, wav_lens):
        """Runs full transcription - note: no gradients through decoding"""
        return self.transcribe_batch(wavs, wav_lens)


@dataclass
class TransducerASRStreamingContext:
    config: DynChunkTrainConfig
    fea_extractor_context: Any
    encoder_context: Any
    decoder_hidden: Optional[torch.Tensor]


class TransducerASRStreamingWrapper:
    def __init__(self, modules, hparams):
        self.modules = modules
        self.hparams = hparams

        self.fea_extractor = hparams.fea_streaming_extractor
        self.filter_props = self.fea_extractor.properties

    def make_streaming_context(self, dynchunktrain_config: DynChunkTrainConfig):
        return TransducerASRStreamingContext(
            config=dynchunktrain_config,
            fea_extractor_context=self.hparams.fea_streaming_extractor.make_streaming_context(),
            encoder_context=self.hparams.Transformer.make_streaming_context(
                dynchunktrain_config=dynchunktrain_config,
                encoder_kwargs={
                    "mha_left_context_size": dynchunktrain_config.left_context_size
                    * dynchunktrain_config.chunk_size
                },
            ),
            decoder_hidden=None,
        )

    def get_chunk_size_frames(
        self, dynchunktrain_config: DynChunkTrainConfig
    ) -> int:
        """Chunk size in actual audio samples that the user should forward to
        `encode_chunk`."""
        return (self.filter_props.stride - 1) * dynchunktrain_config.chunk_size

    def decode_preserve_leading_space(self, hyps):
        """Assuming the tokenizer is sentencepiece, decodes the input hypothesis
        but preserves initial spaces as we likely want to keep them in a
        streaming setting."""

        protos = self.hparams.tokenizer.decode(hyps, out_type="immutable_proto")
        texts = [proto.text for proto in protos]

        for i, batch in enumerate(protos):
            if len(batch.pieces) >= 1:
                if batch.pieces[0].piece[0] == "\u2581":
                    texts[i] = " " + texts[i]

        return texts

    @torch.no_grad
    def encode_chunk(
        self,
        context: TransducerASRStreamingContext,
        chunk: torch.Tensor,
        chunk_len: Optional[torch.Tensor] = None,
    ):
        """Encoding of a batch of audio chunks into a batch of encoded
        sequences.
        Must be called over a given context in the correct order of chunks over
        time.

        Arguments
        ---------
        context : TransducerASRStreamingContext
            Mutable streaming context object, which must be specified and reused
            across calls when streaming.
            You can obtain an initial context by calling
            `asr.streamer.make_streaming_context(config)`.

        chunk : torch.Tensor
            The tensor for an audio chunk of shape `[batch size, time]`.
            The time dimension must strictly match
            `get_chunk_size_frames(config)`.
            The waveform is expected to be in the model's expected format (i.e.
            the sampling rate must be correct).

        chunk_len : Optional[torch.Tensor]
            The relative chunk length tensor of shape `[batch size]`. This is to
            be used when the audio in one of the chunks of the batch is ending
            within this chunk.
            If unspecified, equivalent to `torch.ones((batch_size,))`.

        Returns
        -------
        torch.Tensor
            Encoded output, of a model-dependent shape."""
        assert chunk.shape[-1] <= self.get_chunk_size_frames(context.config)

        x = self.fea_extractor(
            chunk, context=context.fea_extractor_context, lens=chunk_len
        )
        x = self.modules.enc.forward_streaming(x, context.encoder_context)
        x = self.modules.proj_enc(x)
        return x

    @torch.no_grad
    def decode_chunk(
        self, context: TransducerASRStreamingContext, x: torch.Tensor
    ) -> tuple[list, list]:
        """Decodes the output of the encoder into tokens and the associated
        transcription.
        Must be called over a given context in the correct order of chunks over
        time.
        
        Arguments
        ---------
        context : TransducerASRStreamingContext
            Mutable streaming context object, which should be the same object
            that was passed to `encode_chunk`.

        x : torch.Tensor
            The output of `encode_chunk` for a given chunk.

        Returns
        -------
        list of str
            Decoded tokens of length `batch_size`. The decoded strings can be
            of 0-length.
        list of list of output token hypotheses
            List of length `batch_size`, each holding a list of tokens of any
            length `>=0`.
        """
        (best_hyps, _scores, _, _, h,) = self.hparams.Greedysearcher(
            x, context.decoder_hidden, return_hidden=True
        )
        context.decoder_hidden = h

        best_words = self.decode_preserve_leading_space(best_hyps)

        return best_words, best_hyps


class StreamingTransducerASR(Pretrained):
    """A ready-to-use, streaming-capable transducer model.

    Example
    -------
    >>> from speechbrain.inference.ASR import StreamingTransducerASR
    >>> from speechbrain.utils.dynamic_chunk_training import DynChunkTrainConfig
    >>> tmpdir = getfixture("tmpdir")
    >>> asr_model = StreamingTransducerASR.from_hparams(source="speechbrain/asr-conformer-streaming-librispeech", savedir=tmpdir,) # doctest: +SKIP
    >>> asr_model.transcribe_file("speechbrain/asr-conformer-streaming-librispeech/test-en.wav", DynChunkTrainConfig(24, 8)) # doctest: +SKIP
    """

    HPARAMS_NEEDED = ["fea_streaming_extractor", "Greedysearcher"]
    MODULES_NEEDED = ["enc", "proj_enc"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.streamer = TransducerASRStreamingWrapper(self.mods, self.hparams)

    def transcribe_file_streaming(
        self, path, dynchunktrain_config: DynChunkTrainConfig, **kwargs,
    ):
        """Transcribes the given audio file into a sequence of words, in a
        streaming fashion, meaning that text is being yield from this
        generator, in the form of strings to concatenate.

        At the moment, the file is fully loaded in memory, but processing itself
        is done in chunks.

        Arguments
        ---------
        path : str
            Path to the audio file to trancsribe.

        Returns
        -------
        An iterator yielding transcribed chunks (strings). There is a yield for
        every chunk, even if the transcribed string for that chunk is empty.
        """

        waveform = self.load_audio(path, **kwargs)
        batch = waveform.unsqueeze(0)
        rel_length = torch.tensor([1.0])

        chunk_size = self.streamer.get_chunk_size_frames(dynchunktrain_config)
        chunks = split_fixed_chunks(batch, chunk_size)

        context = self.streamer.make_streaming_context(dynchunktrain_config)

        pred = ""

        for i, chunk in enumerate(chunks):
            predicted_words = self.transcribe_chunk(context, chunk, rel_length)
            pred = predicted_words[0]
            if i == 0:
                # truncate leading space
                yield pred[1:]
            else:
                yield pred

    def transcribe_file(
        self, path, dynchunktrain_config: DynChunkTrainConfig,
    ):
        """Transcribes the given audio file into a sequence of words.
        At the moment, the file is fully loaded in memory, but processing itself
        is done in chunks.

        Arguments
        ---------
        path : str
            Path to audio file to transcribe.

        dynchunktrain_config : DynChunkTrainConfig
            Streaming configuration. Sane values and how much time chunks
            actually represent is model-dependent.

        Returns
        -------
        str
            The audio file transcription produced by this ASR system.
        """

        pred = ""

        for text_chunk in self.transcribe_file_stream(
            path, dynchunktrain_config
        ):
            pred += text_chunk

        return pred

    def encode_chunk(
        self,
        context: TransducerASRStreamingContext,
        chunk: torch.Tensor,
        chunk_len: Optional[torch.Tensor] = None,
    ):
        """Encoding of a batch of audio chunks into a batch of encoded
        sequences.
        For full speech-to-text offline transcription, use `transcribe_batch` or
        `transcribe_file`.
        Must be called over a given context in the correct order of chunks over
        time.

        Arguments
        ---------
        context : TransducerASRStreamingContext
            Mutable streaming context object, which must be specified and reused
            across calls when streaming.
            You can obtain an initial context by calling
            `asr.streamer.make_streaming_context(config)`.

        chunk : torch.Tensor
            The tensor for an audio chunk of shape `[batch size, time]`.
            The time dimension must strictly match
            `asr.streamer.get_chunk_size_frames(config)`.
            The waveform is expected to be in the model's expected format (i.e.
            the sampling rate must be correct).

        chunk_len : Optional[torch.Tensor]
            The relative chunk length tensor of shape `[batch size]`. This is to
            be used when the audio in one of the chunks of the batch is ending
            within this chunk.
            If unspecified, equivalent to `torch.ones((batch_size,))`.

        Returns
        -------
        torch.Tensor
            Encoded output, of a model-dependent shape."""

        if chunk_len is None:
            chunk_len = torch.ones((chunk.size(0),))

        wavs = chunk.float()
        wavs, wav_lens = wavs.to(self.device), chunk_len.to(self.device)

        return self.streamer.encode_chunk(context, wavs, wav_lens)

    def transcribe_chunk(
        self,
        context: TransducerASRStreamingContext,
        chunk: torch.Tensor,
        chunk_len: Optional[torch.Tensor] = None,
    ):
        """Transcription of a batch of audio chunks into transcribed text.
        Must be called over a given context in the correct order of chunks over
        time.

        Arguments
        ---------
        context : TransducerASRStreamingContext
            Mutable streaming context object, which must be specified and reused
            across calls when streaming.
            You can obtain an initial context by calling
            `asr.streamer.make_streaming_context(config)`.

        chunk : torch.Tensor
            The tensor for an audio chunk of shape `[batch size, time]`.
            The time dimension must strictly match
            `asr.streamer.get_chunk_size_frames(config)`.
            The waveform is expected to be in the model's expected format (i.e.
            the sampling rate must be correct).

        chunk_len : Optional[torch.Tensor]
            The relative chunk length tensor of shape `[batch size]`. This is to
            be used when the audio in one of the chunks of the batch is ending
            within this chunk.
            If unspecified, equivalent to `torch.ones((batch_size,))`.

        Returns
        -------
        str
            Transcribed string for this chunk, might be of length zero.
        """

        if chunk_len is None:
            chunk_len = torch.ones((chunk.size(0),))

        chunk = chunk.float()
        chunk, chunk_len = chunk.to(self.device), chunk_len.to(self.device)

        x = self.streamer.encode_chunk(context, chunk, chunk_len)
        predicted_words, predicted_tokens = self.streamer.decode_chunk(
            context, x
        )

        return predicted_words
