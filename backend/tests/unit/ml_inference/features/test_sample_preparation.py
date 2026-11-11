"""Offline tests for unit ml_inference features test_sample_preparation."""


import sys
from fractions import Fraction
from types import SimpleNamespace
import numpy as np
import pytest
from touchline.ml_inference.features.sample_preparation import decode_broadcast_audio


@pytest.mark.parametrize('discontinuity', [False, True])
def test_preparer_normalizes_all_frame_timebases_and_rejects_real_gaps(monkeypatch, discontinuity):
    normalized = []

    class Frame:
        sample_rate = 16000
        time_base = Fraction(1, 1000)
        format = SimpleNamespace(is_planar=True, name='fltp')
        layout = SimpleNamespace(name='stereo')

        def __init__(self, index=0, data=None):
            self.pts = 119500 + index * 1000 + (10 if discontinuity and index == 3 else 0)
            self.data = np.tile(np.array([[.2], [-.2]], np.float32), (1, 16000)) if data is None else data
            self.samples = self.data.shape[1]

        def to_ndarray(self):
            return self.data

        @staticmethod
        def from_ndarray(data, **kwargs):
            return Frame(data=data)

    class Resampler:
        def __init__(self, **kwargs):
            assert kwargs == {'format': 'fltp', 'layout': 'stereo', 'rate': 16000}

        def resample(self, frame):
            if frame is None:
                return []
            assert frame.time_base == Fraction(1, 16000)
            normalized.append(frame)
            return [frame]

    class Container:
        streams = SimpleNamespace(audio=[SimpleNamespace(codec_context=SimpleNamespace(
            layout=SimpleNamespace(channels=[1, 2])))])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def seek(self, *args, **kwargs):
            pass

        def decode(self, stream):
            return iter(Frame(i) for i in range(7))

    monkeypatch.setitem(sys.modules, 'av', SimpleNamespace(open=lambda path: Container(),
                        time_base=1000000, AudioResampler=Resampler, AudioFrame=Frame))
    if discontinuity:
        with pytest.raises(ValueError, match='PTS discontinuity'):
            decode_broadcast_audio('fixture.mkv', 120)
    else:
        audio = decode_broadcast_audio('fixture.mkv', 120)
        assert audio.shape == (80000, 2) and np.isfinite(audio).all()
        assert np.all(audio[:, 0] == .2) and np.all(audio[:, 1] == -.2)
        assert sum(frame.samples for frame in normalized) == 80000
        assert normalized[0].pts == 120 * 16000
        assert normalized[-1].pts + normalized[-1].samples == 125 * 16000
