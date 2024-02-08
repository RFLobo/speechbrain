"""Library for checking the torchaudio backend.

Authors
 * Mirco Ravanelli 2021
"""
import platform
import logging
import torchaudio

logger = logging.getLogger(__name__)


def try_parsing_torchaudio_major_version():
    if not hasattr(torchaudio, "__version__"):
        return None

    version_split = torchaudio.__version__.split(".")

    # expect in format x.y.zwhatever; we care only about x

    if len(version_split) <= 2:
        # not sure how to parse this
        return None

    try:
        version = int(version_split[0])
    except:
        return None

    return version


def check_torchaudio_backend():
    """Checks the torchaudio backend and sets it to soundfile if
    windows is detected.
    """

    torchaudio_major = try_parsing_torchaudio_major_version()

    if torchaudio_major is None:
        logger.warning(
            "Failed to detect torchaudio major version; unsure how to check your setup. We recommend that you keep torchaudio up-to-date."
        )
    elif torchaudio_major >= 2:
        available_backends = torchaudio.list_audio_backends()

        if len(available_backends) == 0:
            logger.warning(
                "SpeechBrain could not find any working torchaudio backend. Audio files may fail to load. Follow this link for instructions and troubleshooting: https://speechbrain.readthedocs.io/en/latest/audioloading.html"
            )
    else:
        logger.warning(
            "This version of torchaudio is old. SpeechBrain no longer supports the torchaudio global backend mechanism, which may cause issues with certain datasets."
        )
        current_system = platform.system()
        if current_system == "Windows":
            logger.warning(
                "Switched audio backend to \"soundfile\" because you are running Windows and you are running an old torchaudio version."
            )
            torchaudio.set_audio_backend("soundfile")
