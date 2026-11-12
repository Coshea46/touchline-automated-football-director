"""Reusable synthetic test doubles and builders; no provider calls."""


from fractions import Fraction
from types import SimpleNamespace
import numpy as np
from touchline.ml_inference.features.media import HTTP_OPTIONS, MediaCursor


PRIVATE_SOURCE = 'https://private.example.test/source.mkv?token=secret-test-token'


class VideoFrame:
    def __init__(self, index, *, fps=25, origin=0):
        self.index = index
        self.pts = origin + index
        self.duration = 1
        self.width = self.height = 16
        self.is_corrupt = False
        self.converted = 0

    def to_ndarray(self, **kwargs):
        assert kwargs == {'format': 'rgb24'}
        self.converted += 1
        return np.full((16, 16, 3), self.index % 250, np.uint8)


class AudioFrame:
    def __init__(self, data, *, pts=0, rate=16000, time_base=None, planar=True):
        self._data = np.asarray(data)
        self.pts, self.sample_rate = pts, rate
        self.time_base = time_base or Fraction(1, rate)
        self.format = SimpleNamespace(is_planar=planar)
        channels = self._data.shape[0] if planar else 2
        self.layout = SimpleNamespace(channels=[None] * channels)
        self.samples = self._data.shape[1] if planar else self._data.size // channels

    def to_ndarray(self):
        return self._data

    @classmethod
    def from_ndarray(cls, data, *, format, layout):
        assert format == 'fltp' and layout in ('mono', 'stereo')
        return cls(data)


class Resampler:
    def __init__(self, owner, **kwargs):
        assert kwargs['rate'] == 16000
        self.owner = owner
        self.frames = []
        owner.resamplers.append(self)

    def resample(self, frame):
        if frame is None:
            self.owner.flushes += 1
            return []
        assert frame.sample_rate == 16000 and frame.time_base == Fraction(1, 16000)
        self.frames.append(frame)
        return [frame]


class Container:
    def __init__(self, frames, video_stream, audio_stream, *, audio_frames=None):
        self.frames = frames
        self.audio_frames = audio_frames
        self.streams = SimpleNamespace(video=[video_stream], audio=[audio_stream])
        self.seek_calls = []
        self.closed = False

    def seek(self, timestamp, **kwargs):
        self.seek_calls.append((timestamp, kwargs))
        if 'stream' not in kwargs and self.audio_frames is not None:
            self.frames = self.audio_frames

    def decode(self, stream):
        return iter(self.frames)

    def close(self):
        self.closed = True


class AV:
    time_base = 1000000
    AudioFrame = AudioFrame

    def __init__(self, *, seconds=10, channels=2, origin=0):
        self.video = [VideoFrame(i, origin=origin * 25) for i in range(seconds * 25)]
        self.video_stream = SimpleNamespace(
            start_time=origin * 25, time_base=Fraction(1, 25), duration=seconds * 25,
            average_rate=25, codec_context=SimpleNamespace(height=16, width=16),
        )
        self.audio_stream = SimpleNamespace(
            codec_context=SimpleNamespace(layout=SimpleNamespace(channels=[None] * channels), sample_rate=16000),
        )
        self.audio = []
        for i in range(seconds * 2):
            mono = np.full(8000, i / 100, np.float32)
            data = mono[None] if channels == 1 else np.stack([mono, -mono])
            self.audio.append(AudioFrame(data, pts=origin * 16000 + i * 8000))
        self.containers = []
        self.resamplers = []
        self.flushes = 0

    def open(self, source, **kwargs):
        assert kwargs == {'timeout': (10, 5), 'options': HTTP_OPTIONS}
        assert source == PRIVATE_SOURCE
        # Every real AV handle exposes both streams. Role comes from stream
        # selection/seek, never which concurrent open happened to finish first.
        container = Container(self.video, self.video_stream, self.audio_stream, audio_frames=self.audio)
        self.containers.append(container)
        return container

    def AudioResampler(self, **kwargs):
        return Resampler(self, **kwargs)


def cursor(av=None, **kwargs):
    av = av or AV()
    return MediaCursor(PRIVATE_SOURCE, av_module=av, **kwargs), av


def collect(worker, permit):
    result = []
    while (worker.next_index + 0.5) / 15 <= permit:
        batch = worker.pull(permit)
        if not batch:
            break
        result.extend(batch)
    return result
