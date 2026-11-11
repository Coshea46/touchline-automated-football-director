"""Reusable synthetic test doubles and builders; no provider calls."""


from dataclasses import dataclass
from types import SimpleNamespace
import numpy as np
from touchline.ml_inference.features.incremental import IncrementalMatchFeatures


@dataclass
class Camera:
    dx: float = 0
    cut: bool = False


@dataclass
class Observation:
    timestamp: float
    source_timestamp: float
    shape: tuple
    detections: tuple
    camera: Camera
    region: np.ndarray


class Detector:
    def __init__(self):
        self.indexes = []
        self.calls = 0

    def detect(self, frames):
        self.calls += 1
        values = [int(frame[0, 0, 0]) for frame in frames]
        self.indexes.extend(values)
        return [(value,) for value in values]


class Analyzer:
    def __init__(self, config):
        self.camera = SimpleNamespace(previous=None)
        self.calls = 0

    def analyze(self, frame, raw, target, source, timings):
        value = int(frame[0, 0, 0])
        camera = Camera()
        if self.camera.previous is not None:
            previous, timestamp = self.camera.previous
            camera = Camera((value - previous) / (source - timestamp), value % 30 == 0)
        self.camera.previous = (value, source)
        self.calls += 1
        timings['pitch_team'] += 0.01
        timings['camera'] += 0.02
        return Observation(target, source, frame.shape, raw, camera, np.zeros((4, 6), np.uint8))


def sound_features(audio, rate, *, duration_seconds):
    assert rate == 16000 and duration_seconds == 5
    result = np.zeros((75, 5), np.float32)
    # Power over channels preserves opposite-phase stereo instead of cancelling.
    for i in range(75):
        chunk = audio[round(i * rate / 15):round((i + 1) * rate / 15)]
        result[i, 0] = np.mean(chunk.astype(np.float64) ** 2)
    result[:, -1] = 1
    return result


class Extractor:
    def __init__(self, **kwargs):
        self.last_result = None
        self.calls = 0
        self.detector = kwargs.get('detector')

    def assemble_observations(self, observations, *, audio):
        # A deliberately full-window decision: even early rows change when
        # the eventual window range changes. Streaming one final row is wrong.
        values = np.array([o.detections[0] for o in observations])
        start = observations[0].timestamp - 0.5 / 15
        source_times = np.array([o.source_timestamp - start for o in observations])
        result = np.zeros((75, 215), np.float32)
        result[:, 0] = values / 255
        result[:, 1] = (values.max() - values.min()) / 255
        result[:, 2] = values.mean() / 255
        result[:, 3] = source_times
        result[1:, 206] = [o.camera.dx for o in observations[1:]]
        result[1:, 208] = [o.camera.cut for o in observations[1:]]
        result[:, 210:215] = audio
        self.calls += 1
        self.last_result = SimpleNamespace(
            features=result,
            timings={'tracking': 0.03, 'pitch_team': 0.04, 'interpolation': 0.05, 'assembly': 0.06},
            diagnostics={'source_timestamps': source_times, 'audio_available_intervals': 75},
        )
        return result


def frame(index):
    return np.full((16, 24, 3), index % 250, np.uint8)


def pts(index, origin=0):
    # Broadcast25fps selected at the15fps interval centre: unequal source dt.
    return np.floor((origin + (index + 0.5) / 15) * 25 + 1e-9) / 25


def stream(detector=None, **kwargs):
    return IncrementalMatchFeatures(
        'match-a', detector or Detector(),
        config=SimpleNamespace(clip_duration_seconds=5),
        extractor_factory=Extractor, analyzer_factory=Analyzer,
        audio_features_fn=sound_features, **kwargs,
    )


def append_range(worker, start, end, *, external=False):
    worker.append_batch(
        range(start, end), [frame(i) for i in range(start, end)],
        [pts(i, worker.timeline_origin_seconds) for i in range(start, end)],
        detections=[(i % 250,) for i in range(start, end)] if external else None,
    )


def fresh_reference(start, end, audio, origin=0):
    analyzer = Analyzer(None)
    timings = {'pitch_team': 0.0, 'camera': 0.0}
    observations = [
        analyzer.analyze(frame(i), (i % 250,), origin + (i + 0.5) / 15, pts(i, origin), timings)
        for i in range(start, end)
    ]
    return Extractor().assemble_observations(observations, audio=sound_features(audio, 16000, duration_seconds=5))
