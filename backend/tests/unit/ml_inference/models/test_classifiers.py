"""Offline tests for unit ml_inference models test_classifiers."""


from types import SimpleNamespace
import numpy as np
import pytest
from touchline.ml_inference.models.classifiers import MockInterestScorer, ResearchInterestScorer, validate_replay_classifier
from tests.support.ml_inference.classifiers import features, metadata, replay_fixture


def test_mock_deterministic_configured_and_default():
    mock = MockInterestScorer({"b": 0.9})
    assert mock.predict_scores({"a": features(), "b": features()}) == {"a": 0.5, "b": 0.9}
    assert mock.score_ids(["a", "b"]) == mock.score_ids(["a", "b"])


def test_missing_real_bundle_fails_before_importing_torch(tmp_path):
    adapter = ResearchInterestScorer(tmp_path / "missing.pt")
    with pytest.raises(FileNotFoundError, match="Portable classifier bundle"):
        adapter.predict_scores({"a": features()})


@pytest.mark.parametrize("input_size", [210, 215])
def test_real_adapter_delegates_saved_preprocessing_and_batch_selection(tmp_path, input_size):
    path = tmp_path / "classifier.pt"
    path.write_bytes(b"synthetic loader fixture, not an actual Torch bundle")
    loads, raw_calls, prepared = [], [], []

    info = metadata()
    info.update(model_input_size=input_size, feature_indices=list(range(input_size)))

    class ResearchAPIFixture:
        metadata = info

        def predict_batch(self, raw):
            # Simulates ownership by the existing portable-bundle API: the adapter
            # must send raw values and let this API apply its saved state exactly once.
            raw_calls.append(raw.copy())
            normalized = raw.copy()
            normalized[..., 0] = (raw[..., 0] - 0.1) / 0.5
            normalized = normalized[..., :input_size]
            prepared.append(normalized)
            return normalized[:, :, 0].mean(axis=1)

    def loader(filename, *, device):
        loads.append((filename, device))
        return ResearchAPIFixture()

    scorer = ResearchInterestScorer(path, loader=loader)
    if input_size == 210:
        with pytest.raises(ValueError, match="full-audio"):
            scorer.prepare()
        return
    result = scorer.predict_scores({"z": features(0.4), "a": features(0.2)})
    assert result["a"] == pytest.approx(0.2)
    assert result["z"] == pytest.approx(0.6)
    assert prepared[0].shape == (2, 75, input_size)
    np.testing.assert_array_equal(raw_calls[0][0], features(0.2))
    scorer.predict_scores({"z": features(0.4)})
    assert loads == [(path, "cpu")]


@pytest.mark.parametrize("field,value", [("input_shape", [75, 210]),
                                        ("sampling_fps", 25),
                                        ("feature_schema_version", "other")])
def test_real_schema_rejected(tmp_path, field, value):
    path = tmp_path / "classifier.pt"
    path.write_bytes(b"fixture")
    info = metadata()
    info[field] = value
    scorer = ResearchInterestScorer(path, loader=lambda *a, **kw: SimpleNamespace(metadata=info))
    with pytest.raises(ValueError, match="schema"):
        scorer.prepare()


@pytest.mark.parametrize("shape,dtype", [((75, 210), np.float32), ((1, 75, 215), np.float32),
                                       ((75, 215), np.float64)])
def test_classifier_feature_validation(shape, dtype, tmp_path):
    scorer = ResearchInterestScorer(tmp_path / "not-required.pt")
    with pytest.raises(ValueError, match="float32"):
        scorer.predict_scores({"a": np.zeros(shape, dtype)})


def test_real_adapter_rejects_bad_batch_result(tmp_path):
    path = tmp_path / "classifier.pt"
    path.write_bytes(b"fixture")
    model = SimpleNamespace(metadata=metadata(), predict_batch=lambda x: np.zeros((len(x), 1)))
    scorer = ResearchInterestScorer(path, loader=lambda *a, **kw: model)
    with pytest.raises(ValueError, match="batch dimension"):
        scorer.predict_scores({"a": features()})


def test_replay_readiness_audits_real_model_structure_without_prediction(tmp_path):
    import hashlib

    scorer, _ = replay_fixture(tmp_path)
    audit = validate_replay_classifier(scorer, model_version="v1/classifier.pt")
    assert audit["bundle_sha256"] == hashlib.sha256(scorer.path.read_bytes()).hexdigest()
    assert audit["model_version"] == "v1/classifier.pt"
    assert audit["architecture"]["num_layers"] == 1
    assert audit["normalization"]["fitted_split"] == "train"
    assert audit["dataset_audit_sha256"] == "a" * 64


@pytest.mark.parametrize("field,value", [("kind", "tcn"), ("hidden_size", 32), ("input_size", 210)])
def test_replay_rejects_wrong_architecture_without_restricting_legacy_adapter(tmp_path, field, value):
    scorer, classifier = replay_fixture(tmp_path)
    classifier.metadata["architecture"][field] = value
    scorer.prepare()  # The legacy adapter remains architecture-generic.
    with pytest.raises(ValueError, match="GRU"):
        validate_replay_classifier(scorer)


@pytest.mark.parametrize("field,value", [("num_layers", 2), ("num_layers", True),
                                        ("bidirectional", True), ("hidden_size", 32), ("batch_first", False)])
def test_replay_checks_actual_gru_instead_of_only_claimed_metadata(tmp_path, field, value):
    scorer, classifier = replay_fixture(tmp_path)
    setattr(classifier.model.gru, field, value)
    with pytest.raises(ValueError, match="one-layer"):
        validate_replay_classifier(scorer)


@pytest.mark.parametrize("field,value", [("fitted_split", "valid"), ("mean", [float("nan")] * 215),
                                        ("scale", [0] * 215), ("count", [0.5] * 215)])
def test_replay_requires_saved_training_normalization(tmp_path, field, value):
    scorer, classifier = replay_fixture(tmp_path)
    classifier.metadata["normalization"][field] = value
    with pytest.raises(ValueError, match="normalization"):
        validate_replay_classifier(scorer)


@pytest.mark.parametrize("field,value", [("dataset_audit_sha256", "unknown"),
                                        ("threshold_selection_split", "test"), ("decision_threshold", float("nan"))])
def test_replay_requires_audited_provenance_and_validation_threshold(tmp_path, field, value):
    scorer, classifier = replay_fixture(tmp_path)
    classifier.metadata[field] = value
    with pytest.raises(ValueError):
        validate_replay_classifier(scorer)
