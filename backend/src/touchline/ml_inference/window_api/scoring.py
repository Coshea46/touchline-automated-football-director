"""Stateless, deterministic direction with no accumulated staying bonus."""

import math

from touchline.ml_inference.config import finite_number
from touchline.ml_inference.features.validation import validate_match_id


def choose_match(current_match_id: str, interest_scores: dict[str, float],
                 staying_bonus: float = 0.10) -> str:
    validate_match_id(current_match_id, "current_match_id")
    bonus = finite_number(staying_bonus, "staying_bonus")
    if not isinstance(interest_scores, dict):
        raise TypeError("interest_scores must be a dictionary")
    ranked = []
    for match_id, score in interest_scores.items():
        validate_match_id(match_id)
        # Interest scores from both supported classifiers are in [0,1].
        score = finite_number(score, f"score for {match_id}")
        if score > 1:
            raise ValueError("Interest scores must lie in [0,1]")
        adjusted = score + (bonus if match_id == current_match_id else 0)
        if not math.isfinite(adjusted):
            raise ValueError("Adjusted score is not finite")
        ranked.append((-adjusted, match_id != current_match_id, match_id))
    return min(ranked)[2] if ranked else current_match_id
