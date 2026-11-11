"""Reusable synthetic test doubles and builders; no provider calls."""


import sys
from fractions import Fraction
from types import SimpleNamespace
import pytest
from touchline.captions.identity import source_identity
from touchline.captions.tracks.pipeline import words_to_track
from touchline.captions.errors import CaptionError


@pytest.fixture
def identity():
    return source_identity("match-1.mp4", '"synthetic-etag"', 100,
                           "https://test.invalid/match-1.mp4?signature=private")


@pytest.fixture
def track(identity):
    return words_to_track({"text": "synthetic fixture", "words": [
        {"start": .25, "end": 1.5, "text": "synthetic fixture", "type": "word", "speaker_id": "speaker_0"},
        {"start": 2, "end": 3, "text": "second fixture", "type": "word", "speaker_id": "speaker_1"}]},
        match_id="cam-1", source_key="match-1.mp4", identity=identity["digest"],
        start=120, end=150, language="en", audio_metadata={"audio_sha256": "fixture"})


class FakeTranscriber:
    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    def transcribe(self, wav, **kwargs):
        self.calls += 1
        if self.fail:
            raise CaptionError("provider_transport_error")
        assert wav == b"fixture-wav"
        assert kwargs == {"model": "scribe_v2", "language": "en"}
        return {"text": "fixture", "words": [{"start": 0, "end": 1, "text": "fixture", "type": "word"}]}


def generate_args(identity):
    return {"match_id": "cam-1", "source_key": "match-1.mp4", "identity": identity,
            "source_url": "https://test.invalid/match-1.mp4", "start_seconds": 120, "end_seconds": 150}


def fake_audio_av(monkeypatch, *, gap_samples=0, missing_tail=False, decoder_error=False):
    import numpy as np

    class Frame:
        def __init__(self, data, pts):
            self.data, self.pts = data, pts
            self.time_base, self.sample_rate = Fraction(1, 16000), 16000
            self.samples = data.shape[1]
            self.format = SimpleNamespace(is_planar=True, name="fltp")
            self.layout = SimpleNamespace(name="stereo", channels=[1, 2])

        def to_ndarray(self):
            return self.data

        @classmethod
        def from_ndarray(cls, data, **kwargs):
            return cls(data, 0)

    frames = [Frame(np.tile(np.array([[.1], [-.2]], dtype=np.float32), (1, 320)), 125 * 16000 - 160),
              Frame(np.tile(np.array([[.3], [-.4]], dtype=np.float32), (1, 320)), 125 * 16000 + 160 + gap_samples)]
    stream = SimpleNamespace(codec_context=SimpleNamespace(layout=frames[0].layout))
    video = SimpleNamespace(start_time=5 * 16000, time_base=Fraction(1, 16000))
    calls = []

    class Container:
        streams = SimpleNamespace(audio=[stream], video=[video])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def seek(self, *args, **kwargs):
            calls.append(("seek", args, kwargs))

        def decode(self, selected):
            assert selected is stream  # no video frames decoded
            if decoder_error:
                raise RuntimeError("PRIVATE_URL")
            return iter(frames[:1] if missing_tail else frames)

    class Resampler:
        def __init__(self, **kwargs):
            assert kwargs == {"format": "fltp", "layout": "stereo", "rate": 16000}

        def resample(self, frame):
            if frame is not None:
                assert frame.time_base == Fraction(1, 16000)
            return [frame] if frame is not None else []

    def open_source(source, **kwargs):
        calls.append(("open", kwargs))
        return Container()

    monkeypatch.setitem(sys.modules, "av", SimpleNamespace(open=open_source, AudioResampler=Resampler,
                                                         AudioFrame=Frame, time_base=1000000))
    return calls
