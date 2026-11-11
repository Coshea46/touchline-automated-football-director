"""Offline tests for unit captions live test_audio."""


import sys
from fractions import Fraction
from types import SimpleNamespace
import numpy as np
from touchline.captions.live.audio import ProgressiveAudioReader


def test_fractional_start_and_permits_keep_exact_native_sample_continuity(monkeypatch):
    rate, origin, frame_size = 16000, 120 * 16000, 160
    samples = np.arange(3200, dtype=np.float32) / 16000
    normalized_inputs = []

    class Frame:
        def __init__(self, data, pts):
            self.data, self.pts = data, pts
            self.samples, self.sample_rate, self.time_base = data.shape[1], rate, Fraction(1, rate)
            self.format = SimpleNamespace(is_planar=True, name="fltp")
            self.layout = SimpleNamespace(name="mono", channels=[1])

        def to_ndarray(self):
            return self.data

        @classmethod
        def from_ndarray(cls, data, **kwargs):
            return cls(data, 0)

    stream = SimpleNamespace(codec_context=SimpleNamespace(layout=SimpleNamespace(channels=[1])))

    class Container:
        def __init__(self):
            self.streams = SimpleNamespace(audio=[stream], video=[SimpleNamespace(start_time=0, time_base=Fraction(1, rate))])

        def seek(self, *args, **kwargs):
            pass

        def decode(self, selected):
            assert selected is stream
            return iter(Frame(samples[a:a + frame_size].reshape(1, -1), origin + a)
                        for a in range(0, len(samples), frame_size))

        def close(self):
            pass

    class StrictResampler:
        def __init__(self, **kwargs):
            self.next_pts = None

        def resample(self, frame):
            if self.next_pts is not None:
                # Mirrors PyAV's requirement; old ceil/floor code fails here.
                assert frame.pts == self.next_pts
            normalized_inputs.append((frame.pts, frame.samples))
            self.next_pts = frame.pts + frame.samples
            return [frame]

    monkeypatch.setitem(sys.modules, "av", SimpleNamespace(open=lambda *a, **k: Container(), time_base=1000000,
                                                         AudioFrame=Frame, AudioResampler=StrictResampler))
    start = 120.000021
    reader = ProgressiveAudioReader("https://test.invalid/source", start)
    chunks = []
    permits = [start + k * .010321 for k in range(1, 13)]
    try:
        for permit in permits:
            chunk = reader.pull(permit)
            if chunk:
                assert chunk.start_seconds >= start
                assert chunk.end_seconds <= permit + 1e-7
                if chunks:
                    assert abs(chunk.start_seconds - chunks[-1].end_seconds) < 1e-9
                chunks.append(chunk)
    finally:
        reader.close()
    first = int(np.ceil(start * rate - 1e-7)) - origin
    last = int(np.floor(permits[-1] * rate + 1e-7)) - origin
    expected = np.rint(samples[first:last] * 32767).astype("<i2").tobytes()
    assert b"".join(chunk.pcm for chunk in chunks) == expected
    assert normalized_inputs[0][0] == origin + first
    assert sum(count for _, count in normalized_inputs) == last - first
