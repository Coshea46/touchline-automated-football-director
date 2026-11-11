"""Explicit audio contracts for replay inference, separate from live captions.

The v3 path adapts the frozen native sample-clock decoder to bounded persistent
readers. Its exact reference source is retained for SHA checks and parity tests;
only the released core_audio helper builds the five model audio channels.
"""

import hashlib

import numpy as np

from touchline.ml_inference.config import PACKAGE_ROOT

LEGACY_AUDIO = 'legacy-gru'
SAMPLECLOCK_AUDIO = 'sampleclock-v3'
DECODER_SHA256 = 'e7772f027ddb2c66f3801329995c8c8a1c9a36e23e0c44d92aeaa7023c75c8a4'
DECODER_REVISION = 'autonomous-native-sample-clock-v1'
CORE_AUDIO_SHA256 = '77918bc5d4a65dc0c157ab37fb4ada6a00be71c4f57eb45ba6362e5af86ab0b1'


def validate_audio_contract(value):
    if value not in (LEGACY_AUDIO, SAMPLECLOCK_AUDIO):
        raise ValueError('Unknown replay audio contract')
    return value


def validate_aligned_pcm(audio):
    """NaN means a real unavailable interval; infinity never means missing."""
    if audio is None:
        return
    if not isinstance(audio, np.ndarray) or audio.dtype != np.float32:
        raise TypeError('Aligned audio must be float32 PCM or explicitly absent')
    if audio.shape not in ((80000,), (80000, 2)):
        raise ValueError('Aligned audio must contain exactly 80000 mono/stereo samples')
    if np.isinf(audio).any() or np.any(np.abs(audio[np.isfinite(audio)]) > 1):
        raise ValueError('Aligned audio must be in [-1,1], with NaN only for real gaps')


def verify_reference_decoder():
    path = PACKAGE_ROOT / 'runtime' / 'audio_sampleclock_v3.py'
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != DECODER_SHA256:
        raise ValueError('Frozen sample-clock decoder source SHA differs')
    return path


def clock_tolerance(stream, rate, frame_time_base):
    """The released decoder's exact codec/container tick envelope."""
    ticks = (3 if getattr(stream.codec_context, 'name', None) == 'vorbis'
             and .00099 <= float(stream.time_base) <= .00101 else 1)
    return max(1, int(np.ceil(ticks * float(frame_time_base) * rate)))


def repair_v3_audio(raw, audio, *, endpoint_seconds):
    """Repair once, preserving visual210 bytes and the caller's original raw."""
    validate_aligned_pcm(audio)
    verify_reference_decoder()
    from matchpulse.research.core_audio import (
        CORE_FEATURE_SOURCE_SHA256,
        AudioInput,
        repair_core_audio,
    )
    from matchpulse.research.core_audio import (
        DECODER_SHA256 as released_decoder_sha,
    )

    if released_decoder_sha != DECODER_SHA256 or CORE_FEATURE_SOURCE_SHA256 != CORE_AUDIO_SHA256:
        raise ValueError('Released core-audio contract differs')
    aligned = AudioInput(audio, 16000 if audio is not None else None,
                         endpoint_seconds - 5, endpoint_seconds, audio is not None, DECODER_SHA256)
    return repair_core_audio(raw, aligned, endpoint_seconds=endpoint_seconds)
