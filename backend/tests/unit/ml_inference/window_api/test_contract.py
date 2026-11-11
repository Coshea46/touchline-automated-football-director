"""Offline tests for unit ml_inference window_api test_contract."""


import importlib
import inspect
import os
import numpy as np
import pytest
from touchline.ml_inference import InferenceConfig, InferenceService
from touchline.ml_inference import select_best_match_video_only as select_best_match
from touchline.ml_inference.models.classifiers import MockInterestScorer


def window(h=16, w=24):
    return np.zeros((75, h, w, 3), np.uint8)


@pytest.fixture(autouse=True)
def clear_ml_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("MATCHPULSE_ML_"):
            monkeypatch.delenv(key)


def test_signature():
    signature = inspect.signature(select_best_match)
    assert list(signature.parameters) == ["current_match_id", "match_frames"]
    assert signature.parameters["current_match_id"].annotation is str
    assert signature.parameters["match_frames"].annotation == dict[str, np.ndarray]
    assert signature.return_annotation is str


@pytest.mark.parametrize("count", [1, 3, 7, 9])
def test_public_mock_and_arbitrary_match_counts(monkeypatch, count):
    monkeypatch.setenv("MATCHPULSE_ML_MODE", "mock")
    frames = {f"match-{i}": window(16 + i, 24 + i) for i in range(count)}
    before = {key: value.copy() for key, value in frames.items()}
    assert select_best_match("match-0", frames) == "match-0"
    for key, value in frames.items():
        np.testing.assert_array_equal(value, before[key])


def test_configured_mock_scores_and_determinism():
    service = InferenceService(InferenceConfig(mode="mock"), scorer=MockInterestScorer({"b": 0.8}))
    assert [service.select_video_only("a", {"a": window(), "b": window()}) for _ in range(3)] == ["b"] * 3


def test_missing_current_and_order_independent_ties(monkeypatch):
    monkeypatch.setenv("MATCHPULSE_ML_MODE", "mock")
    assert select_best_match("missing", {"z": window(), "a": window()}) == "a"
    assert select_best_match("missing", {"a": window(), "z": window()}) == "a"


def test_empty_without_mode():
    assert select_best_match("current", {}) == "current"


def test_mode_must_be_explicit():
    with pytest.raises(ValueError, match="MODE explicitly"):
        select_best_match("a", {"a": window()})


@pytest.mark.parametrize("bad", [
    np.zeros((0, 16, 16, 3), np.uint8),
    np.zeros((74, 16, 16, 3), np.uint8),
    np.zeros((76, 16, 16, 3), np.uint8),
    np.zeros((139, 16, 16, 3), np.uint8),
    np.zeros((75, 16, 16), np.uint8),
    np.zeros((75, 16, 16, 4), np.uint8),
    np.zeros((75, 0, 16, 3), np.uint8),
    np.zeros((75, 15, 16, 3), np.uint8),
])
def test_invalid_shapes_rejected_before_mode_or_dispatch(bad):
    with pytest.raises(ValueError):
        select_best_match("a", {"a": bad})


@pytest.mark.parametrize("dtype", [np.float32, np.int16, np.uint16, np.bool_])
def test_invalid_dtypes(dtype):
    with pytest.raises(TypeError, match="uint8"):
        select_best_match("a", {"a": window().astype(dtype)})


@pytest.mark.parametrize("current,frames,error", [
    (None, {}, TypeError), ("", {}, ValueError), ("a", [], TypeError),
    ("a", {1: window()}, TypeError), ("a", {"": window()}, ValueError),
    ("a", {"a": None}, TypeError),
])
def test_invalid_containers_and_ids(current, frames, error):
    with pytest.raises(error):
        select_best_match(current, frames)


def test_payload_limit():
    service = InferenceService(InferenceConfig(mode="mock", max_window_bytes=100))
    with pytest.raises(ValueError, match="max_window_bytes"):
        service.select_video_only("a", {"a": window()})


def test_fast_mock_never_imports_modal_or_research(monkeypatch):
    monkeypatch.setenv("MATCHPULSE_ML_MODE", "mock")
    original = importlib.import_module

    def blocked(name, *args, **kwargs):
        if name in ("modal", "matchpulse", "ultralytics", "torch"):
            raise AssertionError(f"Unexpected heavyweight import: {name}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", blocked)
    assert select_best_match("a", {"a": window()}) == "a"


@pytest.mark.parametrize("kwargs", [
    {"mode": "auto"}, {"mode": "real"}, {"mode": "real", "checkpoint": "../training.pt"},
    {"mode": "mock", "staying_bonus": float("nan")},
    {"mode": "mock", "worker_concurrency": 0},
    {"mode": "mock", "max_inflight": True},
    {"mode": "mock", "call_timeout_seconds": 0},
    {"mode": "mock", "fp16": "true"},
])
def test_bad_configuration(kwargs):
    with pytest.raises((ValueError, TypeError)):
        InferenceConfig(**kwargs)


def test_environment_configuration(monkeypatch):
    monkeypatch.setenv("MATCHPULSE_ML_MODE", "features_mock")
    monkeypatch.setenv("MATCHPULSE_ML_GPU", "T4")
    monkeypatch.setenv("MATCHPULSE_ML_FP16", "false")
    monkeypatch.setenv("MATCHPULSE_ML_STAYING_BONUS", "0.2")
    monkeypatch.setenv("MATCHPULSE_ML_MAX_INFLIGHT", "3")
    config = InferenceConfig.from_env()
    assert (config.gpu, config.fp16, config.staying_bonus, config.max_inflight) == ("T4", False, 0.2, 3)


@pytest.mark.parametrize("key,value", [("MAX_INFLIGHT", "x"), ("FP16", "yes")])
def test_invalid_environment_values(monkeypatch, key, value):
    monkeypatch.setenv("MATCHPULSE_ML_MODE", "mock")
    monkeypatch.setenv("MATCHPULSE_ML_" + key, value)
    with pytest.raises(ValueError):
        InferenceConfig.from_env()
