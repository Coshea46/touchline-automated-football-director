"""Offline tests for unit captions tracks test_audio."""


import wave
from io import BytesIO
import pytest
from touchline.captions.tracks.audio import decode_audio
from touchline.captions.errors import CaptionError
from tests.support.captions.tracks import fake_audio_av


def test_audio_crop_preserves_stereo_native_source_clock_without_padding(monkeypatch):
    import numpy as np

    calls = fake_audio_av(monkeypatch)
    data, metadata = decode_audio("https://test.invalid/private", 120, 120.02, etag="fixture-etag")
    with wave.open(BytesIO(data)) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getnframes()) == (16000, 2, 320)
        values = np.frombuffer(wav.readframes(320), dtype="<i2").reshape(-1, 2) / 32767
    np.testing.assert_allclose(values[:160], np.tile([.1, -.2], (160, 1)), atol=1 / 32767)
    np.testing.assert_allclose(values[160:], np.tile([.3, -.4], (160, 1)), atol=1 / 32767)
    assert metadata["channel_policy"] == "preserved" and metadata["sample_count"] == 320
    assert metadata["max_source_clock_jitter_seconds"] == 0
    assert calls[0][1]["timeout"] == (10, 5)
    assert calls[0][1]["options"]["headers"] == 'If-Match: "fixture-etag"\r\n'


@pytest.mark.parametrize("kwargs,reason", [
    ({"gap_samples": 160}, "audio_discontinuity"),
    ({"missing_tail": True}, "audio_coverage_missing"),
    ({"decoder_error": True}, "audio_decode_RuntimeError"),
])
def test_audio_missing_or_discontinuous_data_never_becomes_silence(monkeypatch, kwargs, reason):
    fake_audio_av(monkeypatch, **kwargs)
    with pytest.raises(CaptionError, match=f"^{reason}$"):
        decode_audio("https://test.invalid/PRIVATE_URL", 120, 120.02)
