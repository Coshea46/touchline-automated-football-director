"""The fixed upstream API; no timestamps, sessions, URLs or HTTP contract."""

from functools import lru_cache

import numpy as np

from touchline.ml_inference.config import InferenceConfig
from touchline.ml_inference.window_api.service import InferenceService
from touchline.ml_inference.features.validation import validate_inputs, validate_video_inputs


@lru_cache(maxsize=8)
def _service(config):
    return InferenceService(config)


def select_best_match(
    current_match_id: str,
    match_frames: dict[str, np.ndarray],
    match_audio: dict[str, np.ndarray],
) -> str:
    """Choose from synchronized five-second RGB and 16kHz float32 PCM windows.

    Explicit MATCHPULSE_ML_MODE configuration is required for nonempty requests.
    Empty requests retain the current ID. Input timing/colour order are supplied
    by the caller; they cannot be inferred from pixel array shape.
    """
    validate_inputs(current_match_id, match_frames, match_audio)
    if not match_frames:
        return current_match_id
    return _service(InferenceConfig.from_env()).select(current_match_id, match_frames, match_audio)


def select_best_match_video_only(current_match_id: str, match_frames: dict[str, np.ndarray]) -> str:
    """Explicit legacy test path; real classifiers cannot run without audio."""
    validate_video_inputs(current_match_id, match_frames)
    if not match_frames:
        return current_match_id
    return _service(InferenceConfig.from_env()).select_video_only(current_match_id, match_frames)
