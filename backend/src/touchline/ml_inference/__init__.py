"""Opt-in Touchline inference. Importing this package never contacts Modal."""

from touchline.ml_inference.window_api.api import select_best_match, select_best_match_video_only
from touchline.ml_inference.config import InferenceConfig
from touchline.ml_inference.window_api.scoring import choose_match
from touchline.ml_inference.window_api.service import InferenceService

__all__ = ["InferenceConfig", "InferenceService", "choose_match", "select_best_match",
           "select_best_match_video_only"]
