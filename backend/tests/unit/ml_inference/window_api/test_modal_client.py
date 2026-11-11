"""Offline tests for unit ml_inference window_api test_modal_client."""


import numpy as np
import pytest
from touchline.ml_inference import InferenceConfig
from touchline.ml_inference.window_api.modal_client import ModalFeatureExtractor, ModalInterestScorer
from touchline.ml_inference.window_api.transport import decode_window
from tests.support.ml_inference.modal import Handle, sdk_fixture


def test_modal_dispatch_lossless_preserves_pixels_and_config():
    frames = np.random.default_rng(4).integers(0, 256, (75, 16, 24, 3), dtype=np.uint8)
    received = []

    def extract(payload):
        received.append(decode_window(payload))
        return Handle(np.zeros((75, 215), np.float32))

    sdk, lookups, options, parameters = sdk_fixture(feature=extract)
    client = ModalFeatureExtractor(InferenceConfig(mode="features_mock", gpu="T4"), sdk=sdk)
    assert not lookups
    assert client.extract("a", frames).shape == (75, 215)
    client.extract("b", frames)
    np.testing.assert_array_equal(received[0], frames)
    assert len(lookups) == 1 and options[0]["gpu"] == "T4"
    assert options[0]["max_containers"] == 1 and parameters[0]["fp16"] is True


def test_timeout_cancels_input_without_stopping_app():
    handle = Handle(error=TimeoutError())
    sdk, *_ = sdk_fixture(feature=lambda payload: handle)
    client = ModalFeatureExtractor(InferenceConfig(mode="features_mock"), sdk=sdk)
    with pytest.raises(TimeoutError, match="exceeded"):
        client.extract("a", np.zeros((75, 16, 16, 3), np.uint8))
    assert handle.cancelled == [{"terminate_containers": False}]


def test_cancelled_request_never_submits_more_inputs():
    sdk, lookups, *_ = sdk_fixture()
    client = ModalFeatureExtractor(InferenceConfig(mode="features_mock"), sdk=sdk)
    client.cancel_pending()
    with pytest.raises(TimeoutError, match="cancelled"):
        client.extract("a", np.zeros((75, 16, 16, 3), np.uint8))
    assert not lookups


def test_real_modal_cpu_batch_and_readiness_once():
    sdk, lookups, options, parameters = sdk_fixture()
    scorer = ModalInterestScorer(InferenceConfig(mode="real", checkpoint="v1/classifier.pt"), sdk=sdk)
    x = np.zeros((75, 215), np.float32)
    x[:, 214] = 1
    assert scorer.predict_scores({"a": x, "b": x}) == {"a": 0.5, "b": 0.5}
    scorer.predict_scores({"a": x})
    assert lookups == [("matchpulse-inference", "ClassifierWorker")]
    assert "gpu" not in options[0]
    assert parameters == [{"checkpoint_name": "v1/classifier.pt"}]


def test_cpu_readiness_accepts_audit_details_while_preserving_required_contract():
    audit = {"schema": "matchpulse-215-v1", "input_shape": [75, 215],
             "audio_required": True, "model_input_size": 215, "bundle_sha256": "a" * 64,
             "model_version": "v1/classifier.pt", "architecture": {"kind": "gru", "hidden_size": 64}}
    sdk, *_ = sdk_fixture(ready=lambda: Handle(audit))
    assert ModalInterestScorer(InferenceConfig(mode="real", checkpoint="v1/classifier.pt"), sdk=sdk).prepare()
    audit["model_input_size"] = 210
    sdk, *_ = sdk_fixture(ready=lambda: Handle(audit))
    with pytest.raises(ValueError, match="schema"):
        ModalInterestScorer(InferenceConfig(mode="real", checkpoint="v1/classifier.pt"), sdk=sdk).prepare()


def test_missing_remote_checkpoint_raises_instead_of_mocking():
    sdk, *_ = sdk_fixture(ready=lambda: Handle(error=FileNotFoundError("missing classifier.pt")))
    scorer = ModalInterestScorer(InferenceConfig(mode="real", checkpoint="classifier.pt"), sdk=sdk)
    with pytest.raises(FileNotFoundError):
        scorer.prepare()


def test_wrong_deployed_schema_rejected():
    sdk, *_ = sdk_fixture(ready=lambda: Handle({"schema": "other", "input_shape": [75, 215]}))
    scorer = ModalInterestScorer(InferenceConfig(mode="real", checkpoint="classifier.pt"), sdk=sdk)
    with pytest.raises(ValueError, match="schema"):
        scorer.prepare()
