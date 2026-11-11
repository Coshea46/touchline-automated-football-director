"""Reusable synthetic test doubles and builders; no provider calls."""


from types import SimpleNamespace
import numpy as np
from touchline.ml_inference.models.classifiers import ResearchInterestScorer


def metadata():
    return {"feature_schema_version": "matchpulse-215-v1", "input_shape": [75, 215],
            "sampling_fps": 15, "clip_duration_seconds": 5,
            "model_input_size": 215, "feature_indices": list(range(215))}


def features(value=0.25):
    x = np.zeros((75, 215), np.float32)
    x[:, 0] = value
    x[:, 214] = 1
    return x


def replay_fixture(tmp_path):
    path = tmp_path / "classifier.pt"
    path.write_bytes(b"strict-readiness fixture, never a trained model")
    info = metadata()
    info.update(checkpoint_version="matchpulse-interest-v1",
                architecture={"kind": "gru", "input_size": 215, "hidden_size": 64},
                normalization={"version": "masked-channel-zscore-v1", "schema": "matchpulse-215-v1",
                               "fitted_split": "train", "mean": [0] * 215,
                               "scale": [1] * 215, "count": [10] * 215},
                dataset_audit_sha256="a" * 64, decision_threshold=0.5,
                threshold_selection_split="valid")
    gru = SimpleNamespace(input_size=215, hidden_size=64, num_layers=1, bidirectional=False, batch_first=True)

    def no_prediction(*args):
        raise AssertionError("Readiness must not fabricate a prediction")

    classifier = SimpleNamespace(metadata=info, model=SimpleNamespace(gru=gru, training=False),
                                 predict_batch=no_prediction)
    scorer = ResearchInterestScorer(path, loader=lambda *a, **kw: classifier)
    return scorer, classifier
