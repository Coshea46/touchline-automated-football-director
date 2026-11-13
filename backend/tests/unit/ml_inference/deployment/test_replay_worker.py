"""Offline tests for unit ml_inference deployment test_replay_worker."""


import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
import pytest
from touchline.ml_inference.models.release import LEGACY_MODEL_VERSION, NEW_MODEL_VERSION
from tests.support.ml_inference.worker import load_app
from tests.support.ml_inference.client import session, MemoryStore


def test_replay_worker_prefers_l4_with_t4_fallback_in_one_bounded_pool(monkeypatch):
    _, registrations, volumes = load_app(monkeypatch)
    limits = registrations["ReplayWorker"]
    assert limits["gpu"] == ["L4", "T4"] and limits["max_containers"] == 40
    assert registrations["FeatureWorker"]["gpu"] == "T4"
    assert "region" not in limits and "routing_region" not in limits
    assert limits["min_containers"] == 0 and limits["buffer_containers"] == 0
    assert limits["scaledown_window"] == 30 and limits["retries"] == 0
    assert 0 < limits["timeout"] <= 4000 and 0 < limits["startup_timeout"] <= 300
    assert limits["cpu"][1] <= 8 and limits["memory"][1] <= 8192
    assert volumes == ["matchpulse-inference-models"]


def replay_load_fixture(monkeypatch, *, advertised_model=NEW_MODEL_VERSION, load_error=None):
    """Real enter/scorer flow with synthetic CPU modules; no SDK/GPU/model calls."""
    module, _, _ = load_app(monkeypatch)
    events = []
    monkeypatch.setattr(module, "Path", lambda value: Path(value).resolve())
    monkeypatch.setattr(module.models, "reload", lambda: events.append("volume_reload"))
    features = ModuleType("matchpulse.features")
    detector_module = ModuleType("matchpulse.features.detector")
    package = ModuleType("matchpulse")
    package.__path__ = []
    package.features = features
    features.__path__ = []
    features.DetectorConfig = lambda **kwargs: kwargs

    class Detector:
        def __init__(self, config):
            events.append("detector_load")
            assert config["device"] == "cuda:0" and config["half"] is True

        def detect(self, images):
            events.append("detector_warmup")
            assert len(images) == 16 and all(image.shape == (720, 1280, 3) for image in images)

    detector_module.YoloDetector = Detector
    features.detector = detector_module
    for name, value in {"matchpulse": package, "matchpulse.features": features,
                        "matchpulse.features.detector": detector_module}.items():
        monkeypatch.setitem(sys.modules, name, value)
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(setNumThreads=lambda count: None))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: True, get_device_name=lambda index: "NVIDIA L4"),
        set_num_threads=lambda count: None, __version__="offline", version=SimpleNamespace(cuda="offline")))
    monkeypatch.setattr(module, "detector_runtime", lambda detector, half: {
        "detector_device": "cuda:0", "detector_dtype": "torch.float16", "detector_fp16": half})
    scorer = SimpleNamespace(readiness={"model_version": advertised_model})

    def no_prediction(*args, **kwargs):
        raise AssertionError("Preloading must not manufacture model scores")

    scorer.predict_scores = no_prediction
    def loader(target, *, model_version, device):
        events.append("classifier_load")
        assert target == (Path("/models").resolve() / NEW_MODEL_VERSION).resolve()
        assert model_version == NEW_MODEL_VERSION and device == "cpu"
        if load_error is not None:
            raise load_error
        return scorer
    monkeypatch.setattr("touchline.ml_inference.models.classifiers.load_replay_scorer", loader)
    return module, events, scorer


def test_enter_preloads_selected_cpu_gru_after_detector_warmup_and_reuses_cache(monkeypatch):
    module, events, scorer = replay_load_fixture(monkeypatch)
    worker = module.ReplayWorker()
    worker.load()
    assert events == ["detector_load", "detector_warmup", "volume_reload", "classifier_load"]
    spec = session(mode="real", model_version=NEW_MODEL_VERSION, checkpoint=NEW_MODEL_VERSION)
    assert worker._scorer(spec) is scorer
    assert worker._scorer(spec) is scorer
    assert events.count("classifier_load") == events.count("volume_reload") == 1
    assert worker.runtime["selected_model_preloaded"] is True
    assert worker.runtime["preloaded_model_version"] == NEW_MODEL_VERSION
    timings = ("imports_seconds", "detector_load_seconds", "warmup_seconds",
               "classifier_preload_seconds", "initialization_seconds")
    assert all(isinstance(worker.runtime[name], float) and worker.runtime[name] >= 0 for name in timings)
    assert worker.runtime["initialization_seconds"] >= sum(worker.runtime[name] for name in timings[:-1])
    with pytest.raises(ValueError, match="does not match"):
        worker._scorer({**spec, "model_version": "unexpected-model"})
    assert events.count("classifier_load") == 1


@pytest.mark.parametrize("mismatched_model", [True, False])
def test_enter_fails_closed_on_unready_selected_model_without_private_exception_text(monkeypatch, mismatched_model):
    error = None if mismatched_model else FileNotFoundError("https://private.example/?token=never-publish-this")
    module, events, _ = replay_load_fixture(monkeypatch,
        advertised_model="wrong-model" if mismatched_model else NEW_MODEL_VERSION, load_error=error)
    worker = module.ReplayWorker()
    with pytest.raises(RuntimeError, match="^Selected replay model preload failed$") as failure:
        worker.load()
    assert "private.example" not in str(failure.value) and "never-publish-this" not in str(failure.value)
    assert worker.runtime["selected_model_preloaded"] is False
    assert "preloaded_model_version" not in worker.runtime and events.count("classifier_load") == 1


def test_replay_worker_missing_bundle_publishes_only_sanitized_error(monkeypatch):
    module, _, _ = load_app(monkeypatch)
    # Production runs on Linux. Resolve the absolute volume root on Windows as
    # well so this fake runtime exercises the loader failure, not host path rules.
    monkeypatch.setattr(module, "Path", lambda value: Path(value).resolve())
    store = MemoryStore()
    store.values["control:owned-session"] = {"clock_epoch": 4}
    module.modal.Dict = SimpleNamespace(from_name=lambda *a, **kw: store)

    def missing_bundle(target, *, model_version, device):
        assert model_version == NEW_MODEL_VERSION and device == 'cpu'
        raise FileNotFoundError("https://private.example/video?token=never-publish-this")

    monkeypatch.setattr("touchline.ml_inference.models.classifiers.load_replay_scorer", missing_bundle)
    worker = module.ReplayWorker()
    worker.classifiers = {}
    result = worker.run(session(mode="real", model_version=NEW_MODEL_VERSION, checkpoint=NEW_MODEL_VERSION))
    assert result["state"] == "error" and result["reason"] == "checkpoint_unavailable"
    assert result["score_kind"] == "model" and result["error_type"] == "FileNotFoundError"
    assert result['error_phase'] == 'load' and result['error_code'] == 'checkpoint_load_failed'
    assert result["clock_epoch"] == 4 and result["scores"] == []
    assert "never-publish-this" not in json.dumps(result) and "private.example" not in json.dumps(result)
    assert store.values["scores:owned-session"] == result


def test_gpu_checkpoint_mount_resolution_matches_cpu_and_preserves_escape_guard(monkeypatch, tmp_path):
    from touchline.ml_inference.models.classifiers import validate_replay_classifier
    from tests.support.ml_inference.classifiers import replay_fixture

    module, _, _ = load_app(monkeypatch)
    logical_root, storage_root = tmp_path / 'models-mount', tmp_path / 'resolved-volume'
    bundle_root = storage_root / 'v1'
    bundle_root.mkdir(parents=True)
    scorer, _ = replay_fixture(bundle_root)
    scorer.readiness = validate_replay_classifier(scorer, model_version=LEGACY_MODEL_VERSION)
    loaded, reloaded = [], []

    class MountedPath:
        """A portable symlink/junction stand-in; no OS symlink privilege needed."""
        def __fspath__(self):
            return str(logical_root)

        def __truediv__(self, child):
            return storage_root / child

        def resolve(self):
            return storage_root.resolve()

    mount = MountedPath()
    # This reproduces the old GPU condition: target resolves under backing
    # storage while the comparison root still names the unresolved mount.
    old_target = (mount / 'v1/classifier.pt').resolve()
    assert not old_target.is_relative_to(mount)
    monkeypatch.setattr(module, 'Path', lambda value: mount if value == '/models' else Path(value))
    monkeypatch.setattr(module.models, 'reload', lambda: reloaded.append(True))

    def loader(target, *, model_version, device):
        assert target == scorer.path.resolve() and device == 'cpu' and model_version == LEGACY_MODEL_VERSION
        loaded.append(target)
        return scorer

    monkeypatch.setattr('touchline.ml_inference.models.classifiers.load_replay_scorer', loader)
    worker = module.ReplayWorker()
    worker.classifiers = {}
    spec = session(mode='real', model_version=LEGACY_MODEL_VERSION, checkpoint='v1/classifier.pt')
    assert worker._scorer(spec) is scorer
    assert worker._scorer(spec) is scorer  # Cached immutable version is loaded once.
    assert loaded == [scorer.path.resolve()] and reloaded == [True]
    with pytest.raises(ValueError, match='inside inference-only storage'):
        worker._scorer({**spec, 'checkpoint': '../outside.pt'})
    with pytest.raises(ValueError, match='inside inference-only storage'):
        worker._scorer({**spec, 'checkpoint': '.'})
    assert loaded == [scorer.path.resolve()] and reloaded == [True]


@pytest.mark.parametrize('phase,message,code', [
    ('resolve', 'Checkpoint must remain inside inference-only storage', 'checkpoint_path_escape'),
    ('validate', 'Replay requires dataset audit SHA256 provenance', 'checkpoint_dataset_provenance'),
    ('volume_reload', 'https://private.example/media?token=never-publish-this', 'checkpoint_volume_reload_failed'),
])
def test_checkpoint_diagnostics_publish_only_phase_and_static_codes(monkeypatch, phase, message, code):
    module, _, _ = load_app(monkeypatch)
    store = MemoryStore()
    module.modal.Dict = SimpleNamespace(from_name=lambda *a, **kw: store)
    worker = module.ReplayWorker()

    def fail(_):
        worker._scorer_phase = phase
        raise ValueError(message)

    worker._scorer = fail
    result = worker.run(session(mode='real', checkpoint='v1/classifier.pt'))
    assert result['error_phase'] == phase and result['error_code'] == code
    assert 'never-publish-this' not in json.dumps(result) and 'private.example' not in json.dumps(result)
