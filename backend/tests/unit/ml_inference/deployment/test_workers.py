"""Offline tests for unit ml_inference deployment test_workers."""


import hashlib
import json
import sys
from types import SimpleNamespace
import numpy as np
import pytest
from touchline.ml_inference.window_api.transport import encode_window
from tests.paths import ML_ROOT
from tests.support.ml_inference.worker import load_app


def test_worker_resources_and_independent_volume(monkeypatch):
    _, registrations, volumes = load_app(monkeypatch)
    assert registrations["FeatureWorker"]["gpu"] == "T4"
    assert registrations["FeatureWorker"]["max_containers"] == 1
    assert registrations["FeatureWorker"]["min_containers"] == 0
    assert "gpu" not in registrations["ClassifierWorker"]
    assert volumes == ["matchpulse-inference-models"]


def test_cuda_evidence_uses_active_prediction_backend(monkeypatch):
    module, _, _ = load_app(monkeypatch)
    parameter = SimpleNamespace(device="cuda:0", dtype="torch.float16")
    source_model = SimpleNamespace(parameters=lambda: iter([SimpleNamespace(device="cpu")]))
    active_model = SimpleNamespace(parameters=lambda: iter([parameter]))
    backend = SimpleNamespace(model=active_model, fp16=True)
    detector = SimpleNamespace(model=SimpleNamespace(
        model=source_model, predictor=SimpleNamespace(model=backend, device="cuda:0")))
    evidence = module.detector_runtime(detector, True)
    assert evidence["detector_device"] == "cuda:0" and evidence["detector_fp16"]
    parameter.device = "cpu"
    with pytest.raises(RuntimeError, match="CUDA"):
        module.detector_runtime(detector, True)
    parameter.device = "cuda:0"
    parameter.dtype = "torch.float32"
    with pytest.raises(RuntimeError, match="precision"):
        module.detector_runtime(detector, True)


def test_worker_decodes_exact_rgb_and_invokes_completed_window_adapter(monkeypatch):
    module, _, _ = load_app(monkeypatch)
    instances, calls = [], []

    class Extractor:
        def __init__(self, weights, **kwargs):
            instances.append((weights, kwargs))

        def prepare(self):
            return self

        def clear_cache(self):
            pass

        def extract(self, match_id, frames):
            calls.append(frames.copy())
            return np.zeros((75, 215), np.float32)

    monkeypatch.setattr("touchline.ml_inference.features.extraction.ResearchFeatureExtractor", Extractor)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: True, get_device_name=lambda i: "NVIDIA L4"),
        __version__="fixture", version=SimpleNamespace(cuda="fixture")))
    worker = module.FeatureWorker()
    worker.load()
    a = np.full((75, 16, 24, 3), 17, np.uint8)
    b = np.full((75, 16, 24, 3), 99, np.uint8)
    assert worker.extract(encode_window(a)).shape == (75, 215)
    assert worker.extract(encode_window(b)).dtype == np.float32
    assert len(instances) == 1
    np.testing.assert_array_equal(calls[0], a)
    np.testing.assert_array_equal(calls[1], b)


def test_vendored_runtime_is_exact_pinned_artifact():
    root = ML_ROOT / "runtime"
    info = json.loads((root / "provenance.json").read_text())
    assert hashlib.sha256((root / info["wheel_filename"]).read_bytes()).hexdigest() == info["wheel_sha256"]


def test_deployment_rejects_missing_or_wrong_weights(tmp_path):
    from touchline.ml_inference.deployment.packaging import runtime_artifacts

    with pytest.raises(ValueError, match="MATCHPULSE_ML_WEIGHTS"):
        runtime_artifacts(None)
    wrong = tmp_path / "yolo.pt"
    wrong.write_bytes(b"not the established YOLO26s weights")
    with pytest.raises(ValueError, match="checksum mismatch"):
        runtime_artifacts(wrong)
