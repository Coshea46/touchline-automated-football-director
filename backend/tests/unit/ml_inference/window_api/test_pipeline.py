"""Offline tests for unit ml_inference window_api test_pipeline."""


import threading
import time
import numpy as np
import pytest
from touchline.ml_inference import InferenceConfig, InferenceService
from touchline.ml_inference.models.classifiers import MockInterestScorer
from touchline.ml_inference.features.extraction import ResearchFeatureExtractor


def frames(value=0):
    return np.full((75, 16, 24, 3), value, np.uint8)


class RecordingScorer:
    def predict_scores(self, features):
        self.received = features
        return {key: float(value[0, 0]) for key, value in features.items()}


class ControlledExtractor:
    def __init__(self, failures=()):
        self.failures = failures

    def extract(self, match_id, video):
        if match_id in self.failures:
            raise RuntimeError("controlled extraction failure")
        result = np.zeros((75, 215), np.float32)
        result[:, 0] = video[0, 0, 0, 0] / 255
        return result


def test_extraction_mapping_and_repeated_request_isolation():
    scorer = RecordingScorer()
    service = InferenceService(InferenceConfig(mode="features_mock"),
                               extractor=ControlledExtractor(), scorer=scorer)
    assert service.select_video_only("a", {"a": frames(10), "b": frames(250)}) == "b"
    assert scorer.received["b"][0, 0] > scorer.received["a"][0, 0]
    assert service.select_video_only("b", {"a": frames(250), "b": frames(10)}) == "a"
    assert np.all(scorer.received["a"][:, 210:] == 0)


def test_some_extractions_fail_and_current_cannot_receive_bonus_without_score(caplog):
    service = InferenceService(InferenceConfig(mode="features_mock"),
                               extractor=ControlledExtractor(failures={"current"}))
    assert service.select_video_only("current", {"current": frames(), "b": frames(), "a": frames()}) == "a"
    assert "omitted match" in caplog.text


def test_all_fail_retains_current_without_synthetic_predictions(caplog):
    service = InferenceService(InferenceConfig(mode="features_mock"),
                               extractor=ControlledExtractor(failures={"a"}))
    assert service.select_video_only("missing-current", {"a": frames()}) == "missing-current"
    assert "without a prediction" in caplog.text


@pytest.mark.parametrize("kind", ["shape", "dtype", "nonfinite", "audio"])
def test_bad_feature_payload_is_a_failed_match(kind, caplog):
    class BadExtractor:
        def extract(self, *args):
            value = np.zeros((75, 215), np.float32)
            if kind == "shape":
                return value[:, :210]
            if kind == "dtype":
                return value.astype(np.float64)
            value[0, 210 if kind == "audio" else 0] = 1 if kind == "audio" else np.nan
            return value

    service = InferenceService(InferenceConfig(mode="features_mock"), extractor=BadExtractor())
    assert service.select_video_only("current", {"a": frames()}) == "current"
    assert "All extractions failed" in caplog.text


def test_bounded_fanout():
    lock = threading.Lock()
    active = maximum = 0

    class Extractor(ControlledExtractor):
        def extract(self, *args):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.01)
                return super().extract(*args)
            finally:
                with lock:
                    active -= 1

    service = InferenceService(InferenceConfig(mode="features_mock", max_inflight=2), extractor=Extractor())
    assert service.select_video_only("0", {str(i): frames() for i in range(7)}) == "0"
    assert maximum == 2


def test_request_deadline_cancels_only_its_extractor(caplog):
    cancelled = threading.Event()

    class Blocked:
        def extract(self, *args):
            cancelled.wait(1)
            raise TimeoutError()

        def cancel_pending(self):
            cancelled.set()

    config = InferenceConfig(mode="features_mock", request_timeout_seconds=0.02)
    service = InferenceService(config, extractor=Blocked())
    assert service.select_video_only("current", {"a": frames()}) == "current"
    assert cancelled.is_set() and "deadline" in caplog.text


def test_scorer_errors_and_configuration_do_not_become_mock_fallbacks():
    class BrokenScorer:
        def prepare(self):
            raise FileNotFoundError("missing real model")

    config = InferenceConfig(mode="real", checkpoint="classifier.pt")
    service = InferenceService(config, extractor=ControlledExtractor(), scorer=BrokenScorer())
    with pytest.raises(FileNotFoundError):
        service.select("a", {"a": frames()}, {"a": np.zeros(80000, np.float32)})
    with pytest.raises(ValueError, match="Mock scorer"):
        InferenceService(config, scorer=MockInterestScorer())


@pytest.mark.parametrize("scores", [{}, {"unexpected": 0.5}, {"a": np.nan}])
def test_invalid_classifier_results_are_not_fabricated(scores):
    class Scorer:
        def predict_scores(self, features):
            return scores

    service = InferenceService(InferenceConfig(mode="features_mock"),
                               extractor=ControlledExtractor(), scorer=Scorer())
    with pytest.raises(ValueError):
        service.select_video_only("a", {"a": frames()})


def test_research_adapter_reuses_one_detector_and_preserves_window_api(tmp_path):
    weights = tmp_path / "fixture.pt"
    weights.write_bytes(b"fake weights; factory avoids YOLO")
    settings, calls = [], []

    class ResearchFixture:
        def extract(self, video, *, fps):
            calls.append((video.copy(), fps))
            result = np.zeros((75, 215), np.float32)
            result[:, 0] = video[0, 0, 0, 0] / 255
            return result

    def factory(**kwargs):
        settings.append(kwargs)
        return ResearchFixture()

    adapter = ResearchFeatureExtractor(weights, factory=factory)
    first = adapter.extract("a", frames(10))
    second = adapter.extract("b", frames(200))
    assert len(settings) == 1
    assert settings[0]["image_size"] == 640 and settings[0]["batch_size"] == 16
    assert settings[0]["confidence"] == 0.15 and settings[0]["ball_confidence"] == 0.08
    assert settings[0]["half"] is True
    assert [fps for _, fps in calls] == [15, 15]
    assert first[0, 0] < second[0, 0]
    assert np.all(second[:, 210:] == 0)


def test_missing_yolo_weights_never_download(tmp_path):
    adapter = ResearchFeatureExtractor(tmp_path / "missing.pt")
    with pytest.raises(FileNotFoundError, match="automatic download is disabled"):
        adapter.extract("a", frames())


def test_missing_modal_dependency_is_configuration_failure(monkeypatch):
    def unavailable():
        raise RuntimeError("Modal dependency is not installed")

    monkeypatch.setattr("touchline.ml_inference.window_api.modal_client._sdk", unavailable)
    service = InferenceService(InferenceConfig(mode="features_mock"))
    with pytest.raises(RuntimeError, match="not installed"):
        service.select_video_only("a", {"a": frames()})
