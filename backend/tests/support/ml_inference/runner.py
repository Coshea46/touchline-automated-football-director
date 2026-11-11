"""Reusable synthetic test doubles and builders; no provider calls."""


import copy
from collections import deque
from concurrent.futures import Future
from types import SimpleNamespace
import numpy as np
from touchline.ml_inference.streaming import runner as replay_runner
from touchline.ml_inference.features.media import MediaTimelineError, Sample


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1000 + self.now

    def perf_counter(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class InlinePool:
    def __init__(self, **kwargs):
        self.closed = False

    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001 -- reproduce Future exception transport.
            future.set_exception(exc)
        return future

    def shutdown(self, **kwargs):
        self.closed = True


class Harness:
    def __init__(self, monkeypatch, *, matches=2, seconds=30):
        self.clock = Clock()
        monkeypatch.setattr(replay_runner, 'time', self.clock)
        monkeypatch.setattr(replay_runner, 'ThreadPoolExecutor', InlinePool)
        self.spec = {
            'session_id': 'unit-session', 'mode': 'features_mock',
            'model_version': 'features-mock-v1', 'max_duration_seconds': seconds,
            'max_estimated_cost_usd': 1,
            'matches': [{'match_id': f'match-{i}', 'source_url': f'https://example.test/{i}',
                         'offset_seconds': 120 + 10 * i} for i in range(matches)],
        }
        self.cursors, self.features, self.published, self.detected = [], [], [], []
        self.current_permit = 0
        self.epoch = 0
        self.end_index = None
        self.fail_first_reader = False
        self.fail_first_prime = False
        self.fail_audio = False
        self.always_fail_open = False
        self.control_override = None
        owner = self

        class Cursor:
            def __init__(self, url, *, offset_seconds, start_index):
                if owner.always_fail_open:
                    raise OSError('https://private.invalid/video?secret=never-log-this')
                self.key = int(url.rsplit('/', 1)[1])
                self.offset, self.next_index = offset_seconds, start_index
                self.closed = False
                self.fail = owner.fail_first_reader and not owner.cursors
                self.prime_fail = owner.fail_first_prime and not owner.cursors
                self.metadata = {'width': 24, 'height': 16, 'fps': 15, 'has_audio': True}
                self.permits = []
                self.prime_positions = []
                owner.cursors.append(self)

            def prime(self, permit):
                assert permit + 1e-8 >= self.next_index / 15
                self.prime_positions.append(permit)
                if self.prime_fail:
                    raise OSError('https://private.invalid/prime?secret=never-log-this')

            def pull(self, permit, limit=16):
                assert not self.closed and 1 <= limit <= 16
                assert not self.permits or permit >= self.permits[-1]
                self.permits.append(permit)
                if self.fail and self.next_index >= 30:
                    raise MediaTimelineError('True audio timestamp discontinuity')
                if owner.end_index is not None and self.next_index >= owner.end_index:
                    raise EOFError('Source endpoint')
                result = []
                while len(result) < limit and (self.next_index + 0.5) / 15 <= permit + 1e-8:
                    if owner.end_index is not None and self.next_index >= owner.end_index:
                        break
                    rgb = np.zeros((16, 24, 3), np.uint8)
                    rgb[0, 0, 0], rgb[0, 0, 1] = self.next_index % 250, self.key
                    result.append(Sample(self.next_index, rgb, self.offset + self.next_index / 15))
                    self.next_index += 1
                return result

            def audio_window(self, start, end):
                assert end <= owner.current_permit + 1e-8 and end - start == 5
                if owner.fail_audio:
                    raise MediaTimelineError('Audio window is incomplete')
                return np.zeros((80000, 2), np.float32)

            @property
            def stats(self):
                return {'next_sample_index': self.next_index, 'audio_history_bytes': 0}

            def close(self):
                self.closed = True

        class Features:
            def __init__(self, key, detector, **kwargs):
                self.key = key
                self.offset = kwargs['timeline_origin_seconds']
                self.epochs = [kwargs['source_epoch']]
                self.cache = deque(maxlen=75)
                self.appended, self.assembled = [], []
                self.last_result = None
                owner.features.append(self)

            def append_batch(self, indexes, frames, source_times, *, detections):
                assert list(indexes) == list(range(indexes[0], indexes[0] + len(indexes)))
                if self.cache:
                    assert indexes[0] == self.cache[-1] + 1
                for index, rgb, pts, raw in zip(indexes, frames, source_times, detections, strict=True):
                    assert (index + 0.5) / 15 <= owner.current_permit + 1e-8
                    assert pts == self.offset + index / 15
                    assert raw == (int(rgb[0, 0, 1]), index % 250)
                    self.cache.append(index)
                    self.appended.append((self.epochs[-1], index))

            def assemble(self, end, audio):
                assert list(self.cache) == list(range(end - 75, end))
                assert end / 15 <= owner.current_permit + 1e-8
                assert audio.shape == (80000, 2)
                self.assembled.append(end)
                result = np.zeros((75, 215), np.float32)
                result[:, 0] = end
                result[:, 214] = 1
                self.last_result = SimpleNamespace(timings={'total': 0, 'detector': 0})
                return result

            def reset_epoch(self, epoch):
                assert epoch != self.epochs[-1]
                self.epochs.append(epoch)
                self.cache.clear()

            @property
            def stats(self):
                return {'cache_samples': len(self.cache), 'cache_rgb_bytes': 0}

        class Detector:
            def detect(self, frames):
                assert 1 <= len(frames) <= 16
                values = [(int(f[0, 0, 1]), int(f[0, 0, 0])) for f in frames]
                owner.detected.extend((owner.epoch, *value) for value in values)
                return values

        class Scorer:
            def predict_scores(self, tensors):
                assert tensors
                return {key: 0.25 + 0.01 * int(key.rsplit('-', 1)[1]) for key in tensors}

        self.cursor_factory, self.feature_factory = Cursor, Features
        self.detector, self.scorer = Detector(), Scorer()

    def control(self):
        value = {
            'session_id': self.spec['session_id'], 'active': True, 'playing': True,
            'position': self.clock.now, 'clock_epoch': self.epoch, 'expires_at': 2000,
        }
        if self.control_override is not None:
            value.update(self.control_override(self))
        self.current_permit = value['position']
        self.epoch = value['clock_epoch']
        return value

    def run(self):
        return replay_runner.run_replay_session(
            self.spec, self.detector, self.control,
            lambda payload: self.published.append(copy.deepcopy(payload)),
            scorer=self.scorer, cursor_factory=self.cursor_factory,
            feature_factory=self.feature_factory,
        )


def delayed_visual_pool(harness, monkeypatch, *, delay=.2):
    """CPU futures remain pending until due or explicitly awaited after YOLO."""
    futures, events, shutdowns = [], [], []

    class VisualFuture(Future):
        def __init__(self, function, args, kwargs):
            super().__init__()
            self.function, self.args, self.kwargs = function, args, kwargs
            self.features = args[0].features
            self.ready_at = harness.clock.now + delay
            self.features._test_visual_busy = True
            self.add_done_callback(lambda future: setattr(self.features, '_test_visual_busy', False))

        def finish(self):
            if super().done():
                return
            events.append(('visual_result', len(harness.detected)))
            if self.set_running_or_notify_cancel():
                try:
                    self.set_result(self.function(*self.args, **self.kwargs))
                except Exception as exc:  # noqa: BLE001 -- reproduce Future exception transport.
                    self.set_exception(exc)

        def done(self):
            if harness.clock.now >= self.ready_at:
                self.finish()
            return super().done()

        def result(self, timeout=None):
            self.finish()
            return super().result(timeout=timeout)

    class PipelinePool(InlinePool):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.visual = kwargs.get('thread_name_prefix') == 'replay-visual'

        def submit(self, function, *args, **kwargs):
            if not self.visual:
                return super().submit(function, *args, **kwargs)
            features = args[0].features
            assert not any(f.features is features and not Future.done(f) for f in futures)
            events.append(('visual_submit', len(harness.detected)))
            future = VisualFuture(function, args, kwargs)
            futures.append(future)
            return future

        def shutdown(self, **kwargs):
            shutdowns.append((self.visual, kwargs))
            super().shutdown(**kwargs)

    feature_class = harness.feature_factory
    original_stats = feature_class.stats.fget

    def safe_stats(features):
        # In production this property takes the append lock. Calling it while
        # measurements run would block the processing/publication thread.
        assert not getattr(features, '_test_visual_busy', False)
        return original_stats(features)

    monkeypatch.setattr(feature_class, 'stats', property(safe_stats))
    monkeypatch.setattr(replay_runner, 'ThreadPoolExecutor', PipelinePool)
    return futures, events, shutdowns
