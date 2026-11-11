"""Shared, standard-library-only limits for one authorized caption session."""

import math

MAX_AUDIO_SECONDS = 8 * 60 * 60
MAX_COST_USD = 3.60
DEFAULT_AUDIO_SECONDS = MAX_AUDIO_SECONDS
DEFAULT_COST_USD = MAX_COST_USD
LIVE_PRICE_PER_HOUR_USD = .39


def validate_limits(audio_seconds, cost_usd):
    for value, ceiling in ((audio_seconds, MAX_AUDIO_SECONDS), (cost_usd, MAX_COST_USD)):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 < value <= ceiling):
            raise ValueError(f'Caption allowance must be finite and within {MAX_AUDIO_SECONDS} seconds / ${MAX_COST_USD:.2f}')


def wall_allowance_seconds(audio_seconds, cost_usd):
    validate_limits(audio_seconds, cost_usd)
    return min(audio_seconds, cost_usd * 3600 / LIVE_PRICE_PER_HOUR_USD)
