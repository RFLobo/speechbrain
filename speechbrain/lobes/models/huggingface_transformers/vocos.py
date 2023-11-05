"""This lobe enables the integration of huggingface pretrained
Vocos model.

Repository: https://huggingface.co/charactr/vocos-encodec-24khz
Paper: https://arxiv.org/pdf/2306.00814.pdf

Authors
 * Artem Ploujnikov 2023
"""

import torch
from torch import nn
from speechbrain.dataio.dataio import length_to_mask

try:
    from vocos import Vocos as VocosModel
except ImportError:
    MSG = "Please install vocos to use the Vocos model\n"
    MSG += "E.G. run: pip install vocos"
    raise ImportError(MSG)


DEFAULT_SAMPLE_RATE = 24000
BANDWIDTHS = [1.5, 3.0, 6.0, 12.0]


class Vocos(nn.Module):
    """An wrapper for the HuggingFace Vocos model

    Arguments
    ---------
    source : str
        a HuggingFace repository identifier or a path
    revision : str
        the model revision
    bandwidth : int
        the bandwidth values
        Supported:
        1.5, 3.0, 6.0, 12.0
    freeze : bool
        whether or not parameters should be
        frozen

    Example
    -------
    >>> model_hub = "charactr/vocos-encodec-24khz"
    >>> model = Vocos(model_hub)
    >>> tokens = torch.randint(1024, (4, 10, 2))
    >>> length = torch.tensor([1.0, 0.5, 0.75, 1.0])
    >>> audio, out_length = model(tokens, length)
    >>> audio.shape
    torch.Size([4, 3200])
    >>> out_length
    tensor([1.0000, 0.5000, 0.7500, 1.0000])
    """

    def __init__(
        self, source, revision=None, bandwidth=1.5, freeze=True,
    ):
        super().__init__()
        self.source = source
        self.model = VocosModel.from_pretrained(source, revision)
        self.freeze = freeze
        self.bandwidth = bandwidth
        self.bandwidth_id = (
            (torch.tensor(BANDWIDTHS) - bandwidth).abs().argmin().item()
        )
        if self.freeze:
            for param in self.model.parameters():
                param.requires_grad = False

    def forward(self, inputs, length):
        """Converts vocodec tokens to audio

        Arguments
        ---------
        inputs : torch.Tensor
            A tensor of Vocodec tokens
        length : torch.Tensor
            A 1-D tensor of relative lengths

        Returns
        -------
        wavs : torch.Tensor
            A (Batch x Length) tensor of raw waveforms
        lengths : torch.Tensor
            Relative lengths
        """
        with torch.set_grad_enabled(not self.freeze):
            features = self.model.codes_to_features(inputs.permute(2, 0, 1))
            wavs = self.model.decode(
                features,
                bandwidth_id=torch.tensor(
                    [self.bandwidth_id], device=inputs.device
                ),
            )
            mask = length_to_mask(
                length * wavs.size(1), max_len=wavs.size(1), device=wavs.device
            )
            return wavs * mask, length
