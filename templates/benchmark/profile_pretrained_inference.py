"""Example recipes to benchmark SpeechBrain using PyTorch profiling; print and export tensorboard & FlameGraph reports.

This file covers three use cases using audio data to inquire real-time performance of a pretrained model.
    1. Profile CPU time for single audio    w/o scheduler; no report exporting
    2. Real-time measurement                w/ scheduler; w/ tensorboard export
    3. Analyse details (adds overheads)     〃 — adds: FlameGraph data export & FLOPs estimation (only: mm & conv2d)

Author:
    * Andreas Nautsch 2022
"""

from copy import deepcopy
from speechbrain.pretrained import EncoderDecoderASR
from speechbrain.utils.profiling import (
    profile,
    profile_analyst,
    profile_optimiser,
    report_time,
)


if __name__ == "__main__":
    # Model to benchmark and its functions of interest.
    funcs_to_profile = [
        "transcribe_batch"
    ]  # transcribe_file(), encode_batch() & forward() xref: transcribe_batch()
    asr_model = EncoderDecoderASR.from_hparams(
        source="speechbrain/asr-crdnn-rnnlm-librispeech",
        savedir="pretrained_models/asr-crdnn-rnnlm-librispeech",
    )

    # 1. Profile CPU time for a single audio - NB: w/o scheduling (warm-up) -> distorted measurement.
    asr_model_profile = deepcopy(
        asr_model
    )  # by-value initialisation for clean demonstration of different use cases
    profile(
        asr_model_profile, class_hooks=funcs_to_profile
    )  # declare specific function for profiling
    asr_model_profile.transcribe_file(
        "speechbrain/asr-crdnn-rnnlm-librispeech/example.wav"
    )  # runs benchmark also
    report_time(
        asr_model_profile
    )  # prints formatted CPU & CUDA time and returns: cpu_time, cuda_time (total time)
    """result:
    Self CPU time total: 19.260s
    """
    print(
        asr_model_profile.profiler.key_averages().table(
            sort_by="cpu_time_total", row_limit=10
        )
    )  # prints details
    """print(asr_model_profile.profiler.key_averages().table(sort_by="cuda_time_total", row_limit=10))
    -------------------------  ------------  ------------  ------------  ------------  ------------  ------------
                         Name    Self CPU %      Self CPU   CPU total %     CPU total  CPU time avg    # of Calls
    -------------------------- ------------  ------------  ------------  ------------  ------------  ------------
                 aten::linear         0.22%      43.107ms        40.63%        7.825s       7.976ms           981
                    aten::add        26.97%        5.195s        26.98%        5.197s      19.318ms           269
                   aten::lstm         0.15%      27.941ms        26.63%        5.129s     213.717ms            24
                   aten::tanh        25.41%        4.895s        25.41%        4.895s       6.826ms           717
                  aten::addmm        21.78%        4.194s        21.92%        4.222s       5.218ms           809
                 aten::matmul         0.03%       6.042ms        17.94%        3.455s      19.971ms           173
                     aten::mm        17.87%        3.442s        17.87%        3.442s      19.898ms           173
               aten::gru_cell         0.01%       1.497ms         3.24%     623.261ms      27.098ms            23
                   aten::stft         0.12%      22.287ms         1.65%     318.635ms     318.635ms             1
               aten::_fft_r2c         1.53%     295.408ms         1.53%     295.475ms     295.475ms             1
    -------------------------- ------------  ------------  ------------  ------------  ------------  ------------
    Self CPU time total: 19.260s
    """

    # 2. Real-time measurement.
    # Example: prepare a batch sampler - here: naive 1-by-1 sampling (to get a handle on the profiler's step counter).
    example_path = "../../samples/audio_samples/"
    example_wavs = [
        example_path + "example1.wav",
        example_path + "example5.wav",
        example_path + "example6.wav",
        example_path + "nn_training_samples/" + "spk1_snt2.wav",
        example_path + "nn_training_samples/" + "spk2_snt1.wav",
        example_path + "example_fr.wav",
    ]

    # Quick study of the function we want to profile with scheduler (warm-up), so we know where to: profiler.step_num++
    """ see: EncoderDecoderASR.transcribe_batch()
        with torch.no_grad():                                                       # not relevant to: profiler.step()
            wav_lens = wav_lens.to(self.device)                                     # no indicator of a 'step'
            encoder_out = self.encode_batch(wavs, wav_lens)                         # processes a batch at-once
            predicted_tokens, scores = self.mods.decoder(encoder_out, wav_lens)     # ... continues with that batch
            predicted_words = [                                                     # ... continues with that batch
                self.tokenizer.decode_ids(token_seq)                                # 〃
                for token_seq in predicted_tokens                                   # 〃
            ]                                                                       # 〃
        return predicted_words, predicted_tokens                                    # end.
    """
    # => There is no clear way to increment profiler.step_num in-between.
    # So? — Keep in mind: 'hooking' the EncoderDecoderASR.transcribe_batch() as in (1) does the following:
    """ see: speechbrain.utils.profiling.hook_brain.hook()
        prof.start()                    # profiling begins - should be: profiler.step_num = 0
        r = f(*f_args, **f_kwargs)      # profiling runs - scheduled warm-up & recording - profiler.step() to be called
        prof.stop()                     # profiling ends - results are gathered and written
        return r                        # end.
    """
    # => There is no point in profiler.step_num++ before/after the function hook either.
    # So? — let's try a different way :)
    import torch
    from speechbrain.dataio.dataio import read_audio

    # Profiling activity.
    duration = []
    with profile_optimiser() as prof:  # not covered by unittests - since neither a
        for (
            wav
        ) in (
            example_wavs
        ):  # as mentioned, naive sampling (1-by1) - use instead: padding, collate_fn & batching
            signal = read_audio(wav)
            duration.append(signal.shape[0] / 16000)  # fs=16000 kHz
            asr_model.transcribe_batch(signal.unsqueeze(0), torch.tensor([1.0]))
            prof.step()

    cpu_time, cuda_time = report_time(
        prof
    )  # cpu_time: 14686679; cuda_time: 0 (both in us)
    # Self CPU time total: 14.687s
    us_in_s = 1000.0 * 1000.0
    real_time_factor = (
        cpu_time / us_in_s / sum(duration)
    )  # 14.687s / 23.32525s ~ 0.6296 on CPU

    """print(prof.profiler.key_averages().table(sort_by="cpu_time_total", row_limit=10))
    ---------------  ------------  ------------  ...  ------------  ------------  ------------  ------------
               Name    Self CPU %      Self CPU  ...  CPU time avg       CPU Mem  Self CPU Mem    # of Calls
    ---------------  ------------  ------------  ...  ------------  ------------  ------------  ------------
     ProfilerStep*         3.05%     448.366ms   ...        7.343s      55.91 Mb      -3.31 Gb             2
      aten::linear         0.18%      26.458ms   ...       5.400ms       1.07 Gb           0 b          2132
        aten::lstm         0.82%     119.944ms   ...     403.260ms      67.52 Mb    -422.75 Mb            23
       aten::addmm        48.63%        7.142s   ...       3.768ms     192.28 Mb     192.28 Mb          1963
      aten::matmul         0.06%       8.131ms   ...      23.060ms     907.57 Mb      -7.14 Mb           171
          aten::mm        26.73%        3.926s   ...      22.959ms     907.57 Mb     907.57 Mb           171
     aten::gru_...         0.02%       2.990ms   ...      48.463ms       6.56 Mb     -45.94 Mb            21
        aten::tanh         4.93%     723.667ms   ...     385.134us     718.66 Mb     718.66 Mb          1879
         aten::add         3.16%     463.656ms   ...       1.548ms       1.36 Gb       1.36 Gb           338
     aten::conv...         0.00%     294.000us   ...      16.330ms      78.02 Mb           0 b            29
    ---------------  ------------  ------------  .... ------------  ------------  ------------  ------------
    Self CPU time total: 14.687s
    """

    # 3. Analyse details: function stack; input shapes & FLOPs (of matrix multiplication and conv2d)
    with profile_analyst() as analyst:  # not covered by unittests - since neither a
        for (
            wav
        ) in (
            example_wavs
        ):  # as mentioned, naive sampling (1-by-1) - use instead: padding, collate_fn & batching
            signal = read_audio(wav)
            asr_model.transcribe_batch(signal.unsqueeze(0), torch.tensor([1.0]))
            analyst.step()  # NB: will report also: Total MFLOPs

    # FLOPs are reported for: aten::addmm; aten::mm; aten::add; aten::conv2d & aten::mul operations (not for all calls).

    """print(analyst.profiler.key_averages().table(sort_by="cpu_time_total"))
    ------------  ------------  ------------  ...  ------------  ------------  ------------  ------------  ------------
           Name    Self CPU %      Self CPU   ...  CPU time avg       CPU Mem  Self CPU Mem    # of Calls  Total MFLOPs
    ------------  ------------  ------------  ...  ------------  ------------  ------------  ------------  ------------
    ProfilerS...         1.82%     352.905ms  ...        9.713s      55.91 Mb      -3.31 Gb             2            --
      aten::lstm         6.23%        1.210s  ...     476.523ms      67.52 Mb    -425.66 Mb            23            --
    aten::linear         1.13%     219.203ms  ...       4.985ms       1.07 Gb           0 b          2132            --
     aten::addmm        31.24%        6.068s  ...       3.185ms     192.28 Mb     192.28 Mb          1963    162685.780
    aten::matmul         0.20%      38.106ms  ...      22.470ms     907.57 Mb      -7.14 Mb           171            --
        aten::mm        19.48%        3.784s  ...      22.131ms     907.57 Mb     907.57 Mb           171    124637.251
       aten::add        15.02%        2.919s  ...       8.722ms       1.36 Gb       1.36 Gb           338       364.595
      aten::tanh         6.21%        1.206s  ...     642.000us     718.66 Mb     718.66 Mb          1879            --
    aten::uns...         0.47%      91.286ms  ...     599.809us           0 b           0 b          1900            --
    aten::uns...         1.75%     339.939ms  ...     551.764us           0 b           0 b          1900            --
    aten::narrow         1.79%     347.594ms  ...      91.855us           0 b           0 b          7954            --
    aten::gru...         0.08%      15.603ms  ...      32.963ms       6.56 Mb     -45.94 Mb            21            --
    aten::con...         0.01%       1.468ms  ...      15.701ms      78.02 Mb           0 b            29            --
    aten::_co...         0.02%       3.899ms  ...      15.650ms      78.02 Mb           0 b            29            --
    aten::mkl...         2.28%     443.668ms  ...      15.864ms      74.08 Mb           0 b            28            --
    aten::con...         0.00%     571.000us  ...      51.412ms      71.33 Mb           0 b             8     43164.887
     aten::slice         1.93%     375.090ms  ...      47.032us           0 b           0 b          8341            --
         aten::t         0.53%     102.487ms  ...     100.223us           0 b           0 b          2132            --
     aten::stack         0.38%      73.430ms  ...       1.530ms      92.53 Mb           0 b           131            --
    aten::unbind         0.50%      96.225ms  ...       1.401ms           0 b           0 b           142            --
     aten::copy_         1.00%     193.905ms  ...      71.158us           0 b           0 b          2725            --
       aten::mul         0.84%     163.707ms  ...      31.118us     115.14 Mb     115.07 Mb          5737        30.185
      aten::add_         0.90%     173.954ms  ...      44.297us           0 b           0 b          3927            --
    aten::ind...         0.84%     162.545ms  ...     666.635us     104.85 Mb     104.85 Mb           260            --
    aten::rep...         0.01%       1.771ms  ...      16.776ms      35.47 Mb      -2.57 Kb             8            --
     aten::clone         0.05%       9.489ms  ...       1.205ms     177.73 Mb           0 b           110            --
    aten::select         0.65%     125.891ms  ...      40.037us           0 b           0 b          3246            --
    aten::con...         0.01%       2.279ms  ...       2.820ms     167.76 Mb           0 b            45            --
    aten::tra...         0.57%     111.408ms  ...      52.352us           0 b           0 b          2263            --
    aten::expand         0.58%     112.022ms  ...      53.286us           0 b           0 b          2166            --
    aten::lay...         0.01%       1.935ms  ...       2.955ms      74.61 Mb     -41.66 Kb            29            --
    aten::nat...         0.20%      38.952ms  ...       2.888ms      74.65 Mb     -71.33 Mb            29            --
    aten::uns...         0.41%      80.604ms  ...      34.491us           0 b           0 b          2424            --
     aten::tanh_         0.41%      79.992ms  ...      42.572us           0 b           0 b          1879            --
       aten::cat         0.04%       8.510ms  ...     336.852us     120.25 Mb           0 b           223            --
    aten::ref...         0.07%      14.146ms  ...       9.373ms      48.50 Mb     -44.72 Mb             8            --
    aten::sig...         0.36%      70.644ms  ...      12.579us           0 b           0 b          5616            --
      aten::_cat         0.24%      47.075ms  ...     298.691us     120.25 Mb           0 b           223            --
        aten::to         0.06%      12.475ms  ...      99.807us     146.75 Kb           0 b           612            --
    aten::max...         0.00%     545.000us  ...       9.337ms      20.05 Mb     -40.10 Mb             6            --
    aten::max...         0.21%      41.023ms  ...       9.246ms      60.15 Mb      15.57 Mb             6            --
    aten::_to...         0.22%      42.067ms  ...     105.210us     146.75 Kb           0 b           462            --
    aten::conv1d         0.00%     817.000us  ...       2.163ms       6.69 Mb           0 b            21            --
    aten::as_...         0.18%      35.127ms  ...       1.887us           0 b           0 b         18614            --
       aten::bmm         0.13%      25.271ms  ...       1.235ms       3.28 Mb           0 b            21            --
       aten::max         0.06%      12.513ms  ...     284.913us     930.47 Kb      39.38 Kb            69            --
       aten::div         0.04%       8.461ms  ...      62.191us      19.45 Mb      19.45 Mb           283            --
    aten::pad...         0.04%       8.047ms  ...       8.585ms      13.12 Kb           0 b             2            --
     aten::where         0.03%       5.700ms  ...     346.548us       6.42 Mb           0 b            42            --
    aten::log...         0.01%       1.782ms  ...     309.667us      12.82 Mb           0 b            42            --
    aten::lea...         0.07%      12.935ms  ...     391.970us      75.50 Mb      75.50 Mb            33            --
    aten::_lo...         0.06%      11.224ms  ...     267.238us      12.82 Mb      12.82 Mb            42            --
      aten::item         0.05%      10.083ms  ...      20.115us           0 b           0 b           524            --
    aten::emb...         0.01%       1.665ms  ...     249.976us       1.64 Mb           0 b            42            --
    aten::_un...         0.05%       8.915ms  ...      58.763us           0 b           0 b           173            --
      aten::topk         0.05%      10.159ms  ...     441.696us      19.71 Kb      19.71 Kb            23            --
     aten::empty         0.03%       6.453ms  ...       6.421us     335.71 Mb     335.71 Mb          1005            --
       aten::sub         0.02%       3.046ms  ...      60.467us       6.79 Mb       6.79 Mb            90            --
        aten::eq         0.01%       2.526ms  ...     124.548us     172.97 Kb     172.89 Kb            42            --
    aten::bat...         0.00%     199.000us  ...       1.287ms     908.00 Kb           0 b             4            --
    aten::emp...         0.02%       3.622ms  ...      64.244us     171.54 Mb           0 b            78            --
       aten::sum         0.02%       4.827ms  ...     172.621us     723.41 Kb     723.41 Kb            29            --
    aten::_ba...         0.00%     421.000us  ...       1.237ms     908.00 Kb           0 b             4            --
    aten::mas...         0.01%       1.454ms  ...     215.429us     685.31 Kb           0 b            21            --
    aten::nat...         0.01%       2.758ms  ...       1.127ms     908.00 Kb     -24.00 Kb             4            --
    aten::res...         0.01%       2.704ms  ...      47.602us       2.22 Mb           0 b            93            --
    aten::emp...         0.02%       4.423ms  ...       8.673us       7.23 Mb       7.23 Mb           510            --
    aten::squ...         0.02%       4.017ms  ...      48.386us           0 b           0 b            88            --
     aten::index         0.01%       2.562ms  ...     200.619us       6.56 Kb           0 b            21            --
    aten::_s_...         0.02%       3.475ms  ...      94.976us       6.42 Mb           0 b            42            --
    aten::res...         0.02%       3.808ms  ...       0.883us           0 b           0 b          4315            --
    aten::res...         0.02%       3.782ms  ...      13.459us     176.26 Mb     176.26 Mb           281            --
      aten::view         0.02%       3.465ms  ...       6.638us           0 b           0 b           522            --
    aten::uns...         0.02%       3.168ms  ...      39.226us           0 b           0 b            84            --
    aten::_co...         0.00%     106.000us  ...       3.276ms       3.95 Mb           0 b             1            --
    aten::ara...         0.01%       2.026ms  ...      64.900us       2.13 Kb           0 b            50            --
    aten::non...         0.00%     948.000us  ...     154.000us       1.36 Kb           0 b            21            --
    aten::thn...         0.00%      74.000us  ...       3.168ms       3.95 Mb    -284.06 Kb             1            --
    aten::squ...         0.02%       3.031ms  ...      37.417us           0 b           0 b            84            --
    aten::thn...         0.00%     845.000us  ...       3.094ms       4.22 Mb           0 b             1            --
     aten::zeros         0.01%       1.788ms  ...     143.800us       6.38 Mb           0 b            20            --
    aten::sof...         0.01%     972.000us  ...     129.714us     685.31 Kb           0 b            21            --
      aten::stft         0.00%     727.000us  ...       1.353ms       1.40 Mb      -1.39 Mb             2            --
    aten::repeat         0.01%       1.278ms  ...     587.750us     125.62 Kb           0 b             4            --
      aten::mean         0.00%     491.000us  ...     308.500us         960 b         960 b             6            --
    aten::_so...         0.01%       1.752ms  ...      83.429us     685.31 Kb     685.31 Kb            21            --
    aten::exp...         0.00%     800.000us  ...      70.040us           0 b           0 b            25            --
    torchaudi...         0.01%       1.605ms  ...     862.500us     569.38 Kb           0 b             2            --
    aten::_ff...         0.01%       1.315ms  ...     797.000us       1.40 Mb           0 b             2            --
    aten::addmm_         0.01%       1.491ms  ...       1.492ms           0 b           0 b             1            --
    aten::rem...         0.01%       1.491ms  ...      71.000us      13.12 Kb      13.12 Kb            21            --
    aten::non...         0.01%       1.195ms  ...      59.762us       1.36 Kb       1.36 Kb            21            --
    aten::con...         0.00%     627.000us  ...     620.000us     572.50 Kb           0 b             2            --
      aten::mul_         0.01%       1.072ms  ...      25.524us           0 b           0 b            42            --
       aten::pow         0.00%     911.000us  ...     251.750us       2.10 Mb       2.10 Mb             4            --
      aten::div_         0.00%     474.000us  ...      98.500us           0 b         -24 b            10            --
     aten::fill_         0.00%     970.000us  ...      11.829us           0 b           0 b            82            --
    aten::detach         0.00%     798.000us  ...      36.920us           0 b           0 b            25            --
        aten::gt         0.00%     741.000us  ...      21.571us       3.28 Kb       2.66 Kb            42            --
     aten::zero_         0.00%     292.000us  ...      39.650us           0 b           0 b            20            --
    -------------  ------------  ------------ ...  ------------  ------------  ------------  ------------  ------------
    Self CPU time total: 19.427s
    ...
    to: 14.687s - overheads from additional tracking - not all torch-internal optimisations are effective.
    """
