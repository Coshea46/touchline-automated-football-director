"""Offline tests for unit ml_inference window_api test_scoring."""


import numpy as np
import pytest
from touchline.ml_inference.window_api.scoring import choose_match


def test_highest_and_staying_bonus():
    assert choose_match("a", {"a": 0.5, "b": 0.9}) == "b"
    assert choose_match("a", {"a": 0.5, "b": 0.55}) == "a"
    assert choose_match("a", {"a": 0.5, "b": 0.55}, 0) == "b"


def test_current_wins_adjusted_exact_tie():
    assert choose_match("z", {"a": 0.6, "z": 0.5}) == "z"


def test_lexical_ties_and_absent_current():
    assert choose_match("missing", {"z": 0.5, "a": 0.5}) == "a"
    assert choose_match("missing", {}) == "missing"


def test_bonus_never_accumulates_or_mutates_scores():
    scores = {"a": 0.5, "b": 0.7}
    assert [choose_match("a", scores) for _ in range(5)] == ["b"] * 5
    assert scores == {"a": 0.5, "b": 0.7}


@pytest.mark.parametrize("score", [np.nan, np.inf, -np.inf, -0.1, 1.1, True, "0.5", None])
def test_bad_scores(score):
    with pytest.raises((ValueError, TypeError)):
        choose_match("a", {"a": score})


@pytest.mark.parametrize("bonus", [-1, float("inf"), float("nan"), True])
def test_bad_bonus(bonus):
    with pytest.raises((ValueError, TypeError)):
        choose_match("a", {"a": 0.5}, bonus)
