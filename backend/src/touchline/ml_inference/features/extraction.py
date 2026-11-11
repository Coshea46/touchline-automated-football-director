"""Thin adapter over the existing complete-window extractor, never a rewrite."""

from pathlib import Path
from threading import Lock
from typing import Protocol

import numpy as np

from touchline.ml_inference.features.validation import (
    validate_audio,
    validate_features,
    validate_match_id,
    validate_window,
)


class FeatureExtractor(Protocol):
    def extract(self, match_id: str, frames: np.ndarray, audio: np.ndarray) -> np.ndarray: ...


class ResearchFeatureExtractor:
    def __init__(self, weights, *, device="cuda:0", fp16=True, factory=None):
        self.weights = Path(weights)
        self.device, self.fp16, self.factory = device, fp16, factory
        self._extractor = None
        self._lock = Lock()

    def prepare(self):
        if self._extractor is None:
            if not self.weights.is_file():
                raise FileNotFoundError("Existing YOLO26s weights are required; automatic download is disabled")
            factory = self.factory
            if factory is None:
                from matchpulse.features import (
                    DetectorConfig,
                    ExtractorConfig,
                    MatchFeatureExtractor,
                )

                def factory(**settings):
                    return MatchFeatureExtractor(
                        detector_config=DetectorConfig(**settings),
                        config=ExtractorConfig(clip_duration_seconds=5, tracker="botsort"),
                    )
            self._extractor = factory(
                weights=str(self.weights), device=self.device, half=self.fp16,
                image_size=640, batch_size=16, confidence=0.15, ball_confidence=0.08,
                person_class=0, ball_class=32,
            )
        return self

    def extract(self, match_id, frames, audio=None):
        validate_match_id(match_id)
        validate_window(frames)
        if audio is not None:
            validate_audio(audio)
        with self._lock:
            self.prepare()
            # Research creates fresh window-local player/ball trackers on every call.
            settings = {} if audio is None else {"audio": audio, "audio_sample_rate": 16000}
            result = self._extractor.extract(frames, fps=15, **settings)
            validate_features(result, missing_audio=audio is None)
            return result

    def clear_cache(self):
        with self._lock:
            if self._extractor is not None:
                self._extractor.clear_cache()

    @property
    def last_result(self):
        return self._extractor.last_result if self._extractor is not None else None
