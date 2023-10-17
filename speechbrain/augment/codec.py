"""
Codec Augmentation (via torchaudio)

This library offers an interface to codec augmentation techniques implemented in
torchaudio, enhancing audio data processing.
For in-depth guidance and usage examples, please consult the following tutorial:
    https://pytorch.org/audio/stable/tutorials/audio_data_augmentation_tutorial.html

Note: When utilizing FFmpeg2 as the backend, the maximum number of samples that
can be processed at a time is limited to 16. To overcome this restriction and
process a larger number of samples, consider switching to an alternative backend,
such as "sox."



Authors:
- Mirco Ravanelli (2023)
"""

import random
import torch
import torchaudio


class CodecAugment(torch.nn.Module):
    """
    Apply random audio codecs to input waveforms using torchaudio.

    This class provides an interface for applying codec augmentation techniques to audio data.

    Arguments
    ---------
        sample_rate (int): The sample rate of the input waveform.

    Example
    -------
        >>> waveform = torch.rand(16000, 4)
        >>> augmenter = CodecAugment(16000)
        >>> output_waveform = augmenter(waveform)
    """

    def __init__(self, sample_rate=16000):
        super().__init__()
        self.sample_rate = sample_rate
        self.available_format_encoders = [
            ("wav", "pcm_mulaw"),
            ("mp3", None),
            ("webm", "opus"),
            ("webm", "vorbis"),
            ("g722", None),
            ("ogg", "vorbis"),
            ("ogg", "opus"),
        ]

    def apply_codec(self, waveform, format=None, encoder=None):
        """
        Apply the selected audio codec.

        Arguments
        ----------
            waveform (torch.Tensor): Input waveform of shape `[batch, time]`.
            format (str, optional): The audio format to use (e.g., "wav", "mp3"). Default is None.
            encoder (str, optional): The encoder to use for the format (e.g., "opus", "vorbis"). Default is None.

        Returns
        ---------
            torch.Tensor: Coded version of the input waveform of shape `[batch, time]`.
        """
        audio_effector = torchaudio.io.AudioEffector(
            format=format, encoder=encoder
        )
        return audio_effector.apply(waveform, self.sample_rate)

    def forward(self, waveform):
        """
        Apply a random audio codec from the available list.

        Arguments
        ---------
            waveform (torch.Tensor): Input waveform of shape `[batch, time]`.

        Returns
        ---------
            torch.Tensor: Coded version of the input waveform of shape `[batch, time]`.
        """
        format, encoder = random.choice(self.available_format_encoders)
        return self.apply_codec(waveform, format=format, encoder=encoder)
