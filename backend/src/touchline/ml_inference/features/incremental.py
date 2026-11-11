"""Bounded, source-timed observations with the research window builder unchanged.

Only image-dependent measurements survive a window boundary. Tracking, team
fitting, slot selection and interpolation always run over the completed window.
CameraEstimator uses only the preceding frame; its cached pair result is valid
inside every overlapping window. The research builder resets the first camera
row, including a cut immediately before that window.
"""

from collections import OrderedDict
from dataclasses import dataclass, fields, is_dataclass
from numbers import Integral, Real
from threading import RLock
from time import perf_counter

import numpy as np

from touchline.ml_inference.features.audio import (
    DECODER_SHA256,
    LEGACY_AUDIO,
    SAMPLECLOCK_AUDIO,
    repair_v3_audio,
    validate_aligned_pcm,
    validate_audio_contract,
)
from touchline.ml_inference.features.validation import validate_audio, validate_features, validate_match_id

FPS = 15
WINDOW_SAMPLES = 75
STRIDE_SAMPLES = 30


@dataclass
class IncrementalResult:
    features: np.ndarray
    timings: dict
    diagnostics: dict


def fresh_reference_window(
    detector, frames, source_timestamps, audio, *, window_start_seconds,
    config=None, detector_config=None, extractor_factory=None, audio_features_fn=None,
    audio_contract=LEGACY_AUDIO, core_audio_repair_fn=None,
):
    """Independent research assembly with actual PTS and fresh YOLO per window.

    This validation helper deliberately retains its caller's RGB window and
    repeats all vision work. Production should use IncrementalMatchFeatures.
    ``window_start_seconds`` and source PTS share the original video origin.
    Keeping the selected pixels identical isolates feature-cache equivalence;
    a separate source-file decode comparison must verify the sampler itself.
    """
    validate_audio_contract(audio_contract)
    (validate_aligned_pcm if audio_contract == SAMPLECLOCK_AUDIO else validate_audio)(audio)
    frames, source_timestamps = list(frames), np.asarray(source_timestamps, np.float64)
    if len(frames) != WINDOW_SAMPLES or source_timestamps.shape != (WINDOW_SAMPLES,):
        raise ValueError("Reference requires exactly 75 frames and source timestamps")
    if not np.isfinite(source_timestamps).all() or not np.isfinite(window_start_seconds):
        raise ValueError("Reference source timestamps must be finite")
    if any(not isinstance(f, np.ndarray) or f.dtype != np.uint8 or f.ndim != 3
           or f.shape[-1] != 3 or min(f.shape[:2]) < 16 for f in frames):
        raise ValueError("Reference requires uint8 RGB sampled frames")
    if len({f.shape for f in frames}) != 1:
        raise ValueError("Reference frame dimensions must agree")
    times = source_timestamps - float(window_start_seconds)
    targets = (np.arange(WINDOW_SAMPLES) + 0.5) / FPS
    if (np.any(np.diff(times) < 0) or np.any(times > targets + 1e-8)
            or np.any(targets - times > 0.21 + 1e-8)):
        raise ValueError("Reference source PTS do not match the sampling grid")
    if config is None:
        from matchpulse.features.config import ExtractorConfig

        config = ExtractorConfig(clip_duration_seconds=5, tracker="botsort")
    if config.clip_duration_seconds != 5:
        raise ValueError("Reference requires a five-second configuration")
    if extractor_factory is None:
        from matchpulse.features.extractor import MatchFeatureExtractor

        extractor_factory = MatchFeatureExtractor
    if audio_features_fn is None:
        from matchpulse.features.audio import audio_features

        audio_features_fn = audio_features
    fresh = extractor_factory(detector=detector, config=config, detector_config=detector_config)
    started = perf_counter()
    timing = dict.fromkeys(("detector", "tracking", "pitch_team", "camera", "audio",
                           "interpolation", "assembly"), 0.0)
    t = perf_counter()
    predictions = list(fresh.detector.detect(frames))
    if len(predictions) != WINDOW_SAMPLES:
        raise RuntimeError("Fresh reference detector returned an incorrect frame count")
    raw = [tuple(detections) for detections in predictions]
    timing["detector"] = perf_counter() - t
    features, diagnostics = fresh._assemble(frames, raw, times, timing)
    t = perf_counter()
    if audio_contract == SAMPLECLOCK_AUDIO:
        features = (core_audio_repair_fn or repair_v3_audio)(
            features, audio, endpoint_seconds=float(window_start_seconds) + 5,
        )
    else:
        features[:, 210:215] = audio_features_fn(audio, 16000, duration_seconds=5)
    timing["audio"] = perf_counter() - t
    validate_features(features, missing_audio=None if audio_contract == SAMPLECLOCK_AUDIO else False)
    timing["total"] = perf_counter() - started
    diagnostics.update(source_timestamps=times, detector_frames_inferred=75,
                       detector_cache_hits=0,
                       audio_available_intervals=int(features[:, 214].sum()),
                       audio_contract=audio_contract)
    return IncrementalResult(features, timing, diagnostics)


def _array_bytes(value, visited=None):
    """Count unique retained numerical buffers, without retaining RGB frames."""
    visited = set() if visited is None else visited
    identity = id(value)
    if identity in visited:
        return 0
    visited.add(identity)
    if isinstance(value, np.ndarray):
        return value.nbytes
    if is_dataclass(value) and not isinstance(value, type):
        return sum(_array_bytes(getattr(value, f.name), visited) for f in fields(value)) + sum(
            _array_bytes(v, visited)
            for k, v in vars(value).items()
            if k not in {f.name for f in fields(value)}
        )
    if isinstance(value, dict):
        return sum(_array_bytes(v, visited) for v in value.values())
    if isinstance(value, (tuple, list)):
        return sum(_array_bytes(v, visited) for v in value)
    return 0


def _index(value, name):
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
    return int(value)


class IncrementalMatchFeatures:
    """One logical match and source epoch; detectors may be shared by callers.

    ``sample_index`` is on the replay grid. Its target source time is
    ``timeline_origin_seconds + (sample_index + .5) / 15``. Source timestamps
    must use that same origin and retain the displayed frame's actual PTS.
    ``assemble`` takes an EXCLUSIVE end index: 75, then 105, then 135, etc.
    Nonzero initial indexes support replay seeks. Reset the epoch after a seek,
    source change, decoder discontinuity or failed image analysis.

    ``append_batch(..., detections=...)`` accepts an external shared YOLO batch;
    otherwise this instance calls its supplied detector once for the new batch.
    Factories are injectable so ordinary tests need neither CUDA nor research
    runtime dependencies. Default factories import the installed, pinned wheel.
    """

    def __init__(
        self, match_id, detector, *, config=None, detector_config=None,
        source_epoch="0", timeline_origin_seconds=0.0,
        max_cache_bytes=64 * 1024 * 1024,
        extractor_factory=None, analyzer_factory=None, audio_features_fn=None,
        audio_contract=LEGACY_AUDIO, core_audio_repair_fn=None,
    ):
        validate_match_id(match_id)
        validate_match_id(source_epoch, "source_epoch")
        if (isinstance(timeline_origin_seconds, bool)
                or not isinstance(timeline_origin_seconds, Real)
                or not np.isfinite(timeline_origin_seconds)
                or timeline_origin_seconds < 0):
            raise ValueError("timeline_origin_seconds must be finite and nonnegative")
        if isinstance(max_cache_bytes, bool) or not isinstance(max_cache_bytes, Integral):
            raise TypeError("max_cache_bytes must be an integer")
        if max_cache_bytes < 1:
            raise ValueError("max_cache_bytes must be positive")
        if config is None:
            from matchpulse.features.config import ExtractorConfig

            config = ExtractorConfig(clip_duration_seconds=5, tracker="botsort")
        if config.clip_duration_seconds != 5:
            raise ValueError("Incremental inference requires five-second windows at 15 FPS")
        if extractor_factory is None:
            from matchpulse.features.extractor import MatchFeatureExtractor

            extractor_factory = MatchFeatureExtractor
        if analyzer_factory is None:
            from matchpulse.features.observations import VisualAnalyzer

            analyzer_factory = VisualAnalyzer
        if audio_features_fn is None:
            from matchpulse.features.audio import audio_features

            audio_features_fn = audio_features
        self.audio_contract = validate_audio_contract(audio_contract)
        self._repair_audio = core_audio_repair_fn or repair_v3_audio
        self.match_id, self.source_epoch = match_id, source_epoch
        self.timeline_origin_seconds = float(timeline_origin_seconds)
        self.config, self.detector = config, detector
        self.max_cache_bytes = int(max_cache_bytes)
        self._extractor = extractor_factory(
            detector=detector, config=config, detector_config=detector_config,
        )
        self._analyzer_factory, self._audio_features = analyzer_factory, audio_features_fn
        self._lock = RLock()
        self._reset()

    def _reset(self):
        self._analyzer = self._analyzer_factory(self.config)
        self._observations = OrderedDict()
        self._observation_bytes = 0
        self._next_index = None
        self._last_source_timestamp = None
        self._shape = None
        self._last_window_end = None
        self._failed = False
        self._counters = {
            "samples_seen": 0, "detector_frames_inferred": 0,
            "detector_frames_supplied": 0, "detector_calls": 0,
            "detector_cache_hits": 0, "windows_assembled": 0,
        }
        self._pending_samples = 0
        self._pending_timings = {"detector": 0.0, "pitch_team": 0.0, "camera": 0.0, "append": 0.0}
        self.last_result = None
        self._extractor.last_result = None

    def reset_epoch(self, source_epoch, *, timeline_origin_seconds=None):
        """Discard all observations and camera history before reading a new source epoch."""
        validate_match_id(source_epoch, "source_epoch")
        with self._lock:
            if source_epoch == self.source_epoch:
                raise ValueError("A reset requires a new source_epoch identity")
            if timeline_origin_seconds is not None:
                if (isinstance(timeline_origin_seconds, bool)
                        or not isinstance(timeline_origin_seconds, Real)
                        or not np.isfinite(timeline_origin_seconds)
                        or timeline_origin_seconds < 0):
                    raise ValueError("timeline_origin_seconds must be finite and nonnegative")
                self.timeline_origin_seconds = float(timeline_origin_seconds)
            self.source_epoch = source_epoch
            self._reset()

    @property
    def stats(self):
        with self._lock:
            return {
                **self._counters, "match_id": self.match_id, "source_epoch": self.source_epoch,
                "cache_samples": len(self._observations), "cache_rgb_bytes": 0,
                "cache_bytes": self._observation_bytes + _array_bytes(
                    getattr(self._analyzer.camera, "previous", None)
                ),
                "oldest_sample_index": next(iter(self._observations), None),
                "next_sample_index": self._next_index, "failed": self._failed,
            }

    def append(self, sample_index, rgb, source_timestamp, *, detections=None):
        self.append_batch(
            [sample_index], [rgb], [source_timestamp],
            detections=None if detections is None else [detections],
        )

    def append_batch(self, sample_indexes, frames, source_timestamps, *, detections=None):
        """Analyze consecutive new samples exactly once; never cache their RGB pixels."""
        with self._lock:
            if self._failed:
                raise RuntimeError("Image analysis failed; reset the source epoch before retrying")
            indexes, frames, times = list(sample_indexes), list(frames), list(source_timestamps)
            if not indexes or len(indexes) > WINDOW_SAMPLES:
                raise ValueError("Append between one and 75 new samples per batch")
            if len(indexes) != len(frames) or len(times) != len(frames):
                raise ValueError("Sample indexes, frames and source timestamps must align")
            indexes = [_index(i, "sample_index") for i in indexes]
            first = indexes[0] if self._next_index is None else self._next_index
            if indexes != list(range(first, first + len(indexes))):
                raise ValueError("Samples must be consecutive new indexes; reset epoch after a seek")
            shape = self._shape
            previous = self._last_source_timestamp
            for i, frame, timestamp in zip(indexes, frames, times):
                if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8:
                    raise TypeError("Every sampled RGB frame must be a uint8 NumPy array")
                if frame.ndim != 3 or frame.shape[-1] != 3 or min(frame.shape[:2]) < 16:
                    raise ValueError("Every sampled frame must be RGB (H,W,3), dimensions >=16")
                shape = frame.shape if shape is None else shape
                if frame.shape != shape:
                    raise ValueError("Frame dimensions changed; reset the source epoch")
                if (isinstance(timestamp, bool) or not isinstance(timestamp, Real)
                        or not np.isfinite(timestamp)):
                    raise ValueError("Source timestamps must be finite")
                target = self.timeline_origin_seconds + (i + 0.5) / FPS
                if timestamp > target + 1e-8 or target - timestamp > 0.21 + 1e-8:
                    raise ValueError("Source PTS must be displayed at the target, within 210ms")
                if previous is not None and timestamp < previous:
                    raise ValueError("Source timestamps must not go backwards")
                previous = timestamp
            started = perf_counter()
            if detections is None:
                detector_start = perf_counter()
                predictions = list(self.detector.detect(frames))
                self._pending_timings["detector"] += perf_counter() - detector_start
                self._counters["detector_calls"] += 1
            else:
                predictions = list(detections)
            if len(predictions) != len(frames):
                raise RuntimeError("Detector result count does not match the new sample count")
            # Materialize each result before advancing camera state.
            predictions = [tuple(result) for result in predictions]
            self.last_result = None
            self._extractor.last_result = None
            try:
                for i, frame, timestamp, raw in zip(indexes, frames, times, predictions):
                    target = self.timeline_origin_seconds + (i + 0.5) / FPS
                    observation = self._analyzer.analyze(
                        frame, raw, target, float(timestamp), self._pending_timings,
                    )
                    size = _array_bytes(observation)
                    self._observations[i] = (observation, size)
                    self._observation_bytes += size
                    if len(self._observations) > WINDOW_SAMPLES:
                        _, (_, removed_size) = self._observations.popitem(last=False)
                        self._observation_bytes -= removed_size
                    if self.stats["cache_bytes"] > self.max_cache_bytes:
                        raise MemoryError("Observation cache exceeds max_cache_bytes")
                    self._next_index, self._last_source_timestamp = i + 1, float(timestamp)
                    self._shape = frame.shape
                    self._counters["samples_seen"] += 1
                    self._counters["detector_frames_inferred"] += 1
                    self._counters["detector_frames_supplied"] += int(detections is not None)
                    self._pending_samples += 1
            except Exception:
                # A partial camera advance cannot be retried under the same identity.
                self._failed = True
                self._observations.clear()
                self._observation_bytes = 0
                self._analyzer.camera.previous = None
                raise
            self._pending_timings["append"] += perf_counter() - started

    def assemble(self, window_end_index, audio):
        """Build full raw (75,215) features from the completed source-timed window."""
        (validate_aligned_pcm if self.audio_contract == SAMPLECLOCK_AUDIO else validate_audio)(audio)
        end = _index(window_end_index, "window_end_index")
        with self._lock:
            if self._failed:
                raise RuntimeError("Image analysis failed; reset the source epoch before assembling")
            if self._last_window_end is not None and end != self._last_window_end + STRIDE_SAMPLES:
                raise ValueError("Completed windows must advance by exactly 30 samples (two seconds)")
            start = end - WINDOW_SAMPLES
            if start < 0 or list(self._observations) != list(range(start, end)):
                raise ValueError("The complete 75-sample window must be cached with no future samples")
            started = perf_counter()
            observations = [item[0] for item in self._observations.values()]
            audio_start = perf_counter()
            audio_values = (np.zeros((75, 5), np.float32) if self.audio_contract == SAMPLECLOCK_AUDIO
                            else self._audio_features(audio, 16000, duration_seconds=5))
            audio_seconds = perf_counter() - audio_start
            features = self._extractor.assemble_observations(observations, audio=audio_values)
            if self.audio_contract == SAMPLECLOCK_AUDIO:
                audio_start = perf_counter()
                features = self._repair_audio(features, audio,
                    endpoint_seconds=self.timeline_origin_seconds + end / FPS)
                audio_seconds += perf_counter() - audio_start
            validate_features(features, missing_audio=None if self.audio_contract == SAMPLECLOCK_AUDIO else False)
            research = self._extractor.last_result
            timings = dict(research.timings)
            for key in ("detector", "pitch_team", "camera"):
                timings[key] = timings.get(key, 0.0) + self._pending_timings[key]
            timings["audio"] = audio_seconds
            timings["append"] = self._pending_timings["append"]
            timings["window_assembly"] = perf_counter() - started
            timings["total"] = timings["append"] + timings["window_assembly"]
            new_samples = min(self._pending_samples, WINDOW_SAMPLES)
            hits = WINDOW_SAMPLES - new_samples
            self._counters["detector_cache_hits"] += hits
            self._counters["windows_assembled"] += 1
            diagnostics = dict(research.diagnostics)
            diagnostics.update(
                match_id=self.match_id, source_epoch=self.source_epoch,
                window_start_index=start, window_end_index=end,
                window_start_seconds=start / FPS, window_end_seconds=end / FPS,
                source_start_seconds=self.timeline_origin_seconds + start / FPS,
                source_end_seconds=self.timeline_origin_seconds + end / FPS,
                detector_frames_inferred=new_samples, detector_cache_hits=hits,
                observation_cache_hits=hits, cache=self.stats,
                audio_contract=self.audio_contract,
                audio_decoder_sha256=DECODER_SHA256 if self.audio_contract == SAMPLECLOCK_AUDIO else None,
                audio_available_intervals=int(features[:, 214].sum()),
            )
            self.last_result = IncrementalResult(features, timings, diagnostics)
            self._last_window_end = end
            self._pending_samples = 0
            self._pending_timings = dict.fromkeys(self._pending_timings, 0.0)
            return features
