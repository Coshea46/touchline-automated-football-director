"""Offline tests for unit ml_inference deployment test_replay_worker."""


import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from touchline.ml_inference.models.release import LEGACY_MODEL_VERSION, NEW_MODEL_VERSION
from tests.support.ml_inference.worker import load_app
from tests.support.ml_inference.client import session, MemoryStore


def test_replay_worker_has_one_gpu_pool_zero_idle_and_no_retry(monkeypatch):
    _, registrations, volumes = load_app(monkeypatch)
    limits = registrations["ReplayWorker"]
    assert limits["gpu"] == "T4" and limits["max_containers"] == 4
    assert limits["min_containers"] == 0 and limits["buffer_containers"] == 0
    assert limits["scaledown_window"] == 5 and limits["retries"] == 0
    assert 0 < limits["timeout"] <= 4000 and 0 < limits["startup_timeout"] <= 300
    assert limits["cpu"][1] <= 8 and limits["memory"][1] <= 8192
    assert volumes == ["matchpulse-inference-models"]


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
