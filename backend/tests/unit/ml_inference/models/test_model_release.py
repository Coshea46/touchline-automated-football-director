"""Offline tests for unit ml_inference models test_model_release."""


import importlib.util
import json
import shutil
import sys
from types import SimpleNamespace
import numpy as np
import pytest
from touchline.ml_inference.models import classifiers
from touchline.ml_inference.models.classifiers import SelectedTemporalScorer, load_replay_scorer
from touchline.ml_inference.models.release import CHECKPOINT_SHA256, LEGACY_CHECKPOINT_SHA256, LEGACY_MODEL_VERSION, NEW_MODEL_VERSION, RELEASE_ROOT, SELECTED_HEAD, SMOKE_ATOL, SOURCE_SHA256, verify_release_support
from tests.paths import ML_FIXTURES


CHECKPOINT = ML_FIXTURES / "selected_release" / "classifier.pt"


def fake_release(monkeypatch):
    """An explicitly synthetic API fixture tests adapter delegation, not model quality."""
    calls = []
    expected = json.loads((RELEASE_ROOT / "smoke-verification.json").read_text())["expected_three_scores"][1]
    gru = SimpleNamespace(input_size=68, hidden_size=16, num_layers=1,
                          batch_first=True, bidirectional=False)
    model = SimpleNamespace(training=False, gru=gru,
                            parameters=lambda: [SimpleNamespace(numel=lambda: 4179)])
    classifier = SimpleNamespace(model=model, selected_head=SELECTED_HEAD,
                                 metadata={"score_semantics": "uncalibrated fixture"}, threshold=0.0624)

    class Observation:
        def __init__(self, match_id, half, endpoint_seconds, *, raw215):
            self.match_id, self.half, self.endpoint_seconds, self.raw215 = match_id, half, endpoint_seconds, raw215

    class Predictor:
        kind = "raw215"

        def __init__(self):
            self.model = classifier

        def predict(self, observation):
            calls.append(observation)
            return expected

    predictor = Predictor()
    loads = []

    def loader(directory, **kwargs):
        loads.append((directory, kwargs))
        return predictor

    monkeypatch.setitem(sys.modules, "matchpulse.research.predictor",
                        SimpleNamespace(Observation=Observation, load_selected_predictor=loader))
    monkeypatch.setattr(classifiers, "SOURCE_SHA256", {})
    return predictor, classifier, calls, loads


def test_selected_checkpoint_and_published_fixture_are_immutable():
    assert classifiers.checkpoint_sha256(CHECKPOINT) == CHECKPOINT_SHA256
    observation, verification = verify_release_support()
    assert observation["observation_end_seconds"] == 5
    assert verification["head_order"][1] == SELECTED_HEAD
    assert np.load(RELEASE_ROOT / "smoke-raw.npy", allow_pickle=False).shape == (75, 215)


def test_release_support_tampering_fails_before_prediction(tmp_path):
    for path in RELEASE_ROOT.glob("smoke-*"):
        shutil.copyfile(path, tmp_path / path.name)
    with (tmp_path / "smoke-raw.npy").open("ab") as stream:
        stream.write(b"altered")
    with pytest.raises(ValueError, match="checksum"):
        verify_release_support(tmp_path)


def test_selected_readiness_delegates_exact_pinned_loader_and_real_smoke(monkeypatch):
    _, _, calls, loads = fake_release(monkeypatch)
    scorer = load_replay_scorer(CHECKPOINT)
    assert len(calls) == 1 and calls[0].match_id.startswith("england_epl/")
    assert loads == [(CHECKPOINT.parent, {"expected_manifest_sha256": CHECKPOINT_SHA256,
                                         "head": SELECTED_HEAD, "release_root": CHECKPOINT.parent})]
    assert scorer.readiness["model_version"] == NEW_MODEL_VERSION
    assert scorer.readiness["architecture"]["parameter_count"] == 4179
    assert scorer.readiness["smoke"]["passed"]
    assert scorer.audio_contract == "sampleclock-v3"
    assert classifiers.validate_replay_classifier(scorer, model_version=NEW_MODEL_VERSION) == scorer.readiness
    scorer.prepare()
    assert len(loads) == len(calls) == 1


def test_selected_forwards_raw_once_with_source_endpoints_and_no_history_commit(monkeypatch):
    predictor, _, calls, _ = fake_release(monkeypatch)
    predictor.commit = lambda *args: pytest.fail("Stateless temporal predictor must not commit history")
    scorer = load_replay_scorer(CHECKPOINT)
    raw = np.load(RELEASE_ROOT / "smoke-raw.npy", allow_pickle=False)
    missing_audio = raw.copy()
    missing_audio[:, 210:] = 0
    original = raw.tobytes()
    scorer.predict_scores({"b": missing_audio, "a": raw}, endpoint_seconds={"a": 125, "b": 130})
    assert [(o.match_id, o.endpoint_seconds) for o in calls[1:]] == [("a", 125), ("b", 130)]
    assert calls[1].raw215 is raw and calls[2].raw215 is missing_audio
    assert raw.tobytes() == original


@pytest.mark.parametrize("endpoint", [None, True, float("nan"), 4.999, {"wrong": 5}])
def test_selected_rejects_missing_invalid_or_unmatched_endpoint_before_runtime_load(endpoint):
    raw = np.zeros((75, 215), np.float32)
    with pytest.raises((TypeError, ValueError)):
        SelectedTemporalScorer("missing.pt").predict_scores({"a": raw}, endpoint_seconds=endpoint)


def test_selected_readiness_fails_closed_for_score_architecture_and_source_mismatch(monkeypatch):
    predictor, _, _, _ = fake_release(monkeypatch)
    predictor.predict = lambda observation: 0.9
    with pytest.raises(ValueError, match="parity"):
        load_replay_scorer(CHECKPOINT)
    predictor.model.model.training = True
    with pytest.raises(ValueError, match="architecture"):
        load_replay_scorer(CHECKPOINT)
    monkeypatch.setattr(classifiers, "SOURCE_SHA256", {"fake": "0" * 64})
    monkeypatch.setattr(classifiers.importlib, "import_module", lambda name: SimpleNamespace(__file__=CHECKPOINT))
    with pytest.raises(ValueError, match="source SHA"):
        load_replay_scorer(CHECKPOINT)


def test_selected_never_silently_falls_back_or_accepts_wrong_bytes(tmp_path):
    path = tmp_path / "classifier.pt"
    path.write_bytes(b"not the released checkpoint")
    with pytest.raises(ValueError, match="SHA256"):
        load_replay_scorer(path)
    with pytest.raises(ValueError, match="Unsupported"):
        load_replay_scorer(path, model_version="unknown/classifier.pt")
    with pytest.raises(ValueError, match="CPU"):
        load_replay_scorer(path, device="cuda")


def test_explicit_legacy_factory_keeps_old_adapter_and_strict_audit(monkeypatch):
    sentinel = SimpleNamespace(audio_contract="legacy-gru")
    monkeypatch.setattr(classifiers, "ResearchInterestScorer", lambda path, device: sentinel)
    monkeypatch.setattr(classifiers, "checkpoint_sha256", lambda path: LEGACY_CHECKPOINT_SHA256)
    monkeypatch.setattr(classifiers, "validate_replay_classifier", lambda scorer, model_version: {"model_version": model_version})
    scorer = load_replay_scorer("legacy.pt", model_version=LEGACY_MODEL_VERSION)
    assert scorer is sentinel and scorer.readiness["model_version"] == LEGACY_MODEL_VERSION


def test_installer_rejects_wrong_selected_version_bytes_before_sdk(tmp_path):
    from touchline.tools.cloud.checkpoints import install_checkpoint

    path = tmp_path / "classifier.pt"
    path.write_bytes(b"not a released model")
    digest = classifiers.checkpoint_sha256(path)
    with pytest.raises(ValueError, match="immutable checkpoint"):
        install_checkpoint(path, version=NEW_MODEL_VERSION.split("/")[0], expected_sha256=digest,
                           allow_cloud=True, sdk=object())


@pytest.mark.parametrize("broken", [None, "selected_head", "source_sha256", "smoke"])
def test_installer_accepts_only_complete_selected_cpu_audit(monkeypatch, broken):
    from touchline.tools.cloud.checkpoints import install_checkpoint

    fake_release(monkeypatch)
    audit = load_replay_scorer(CHECKPOINT).readiness.copy()
    audit["source_sha256"] = SOURCE_SHA256
    if broken:
        audit.pop(broken)
    lookups = []

    def lookup(app, name):
        lookups.append((app, name))

        def worker(**kwargs):
            assert kwargs == {"checkpoint_name": NEW_MODEL_VERSION}
            handle = SimpleNamespace(get=lambda **kwargs: audit)
            return SimpleNamespace(ready=SimpleNamespace(spawn=lambda: handle))

        return worker

    sdk = SimpleNamespace(Cls=SimpleNamespace(from_name=lookup))
    kwargs = {"version": NEW_MODEL_VERSION.split("/")[0], "expected_sha256": CHECKPOINT_SHA256,
              "allow_cloud": True, "verify_only": True, "sdk": sdk}
    if broken:
        with pytest.raises(ValueError, match="source/head/fixture"):
            install_checkpoint(CHECKPOINT, **kwargs)
    else:
        assert install_checkpoint(CHECKPOINT, **kwargs)["readiness_verified"]
    assert lookups == [("matchpulse-inference", "ClassifierWorker")]


@pytest.mark.skipif(importlib.util.find_spec("torch") is None or importlib.util.find_spec("matchpulse") is None,
                    reason="Exact released wheel and Torch are required for actual checkpoint smoke")
def test_real_released_checkpoint_matches_published_score_and_remains_stateless():
    scorer = load_replay_scorer(CHECKPOINT)
    assert scorer.readiness["source_sha256"] == SOURCE_SHA256
    assert scorer.readiness["smoke"]["absolute_difference"] <= SMOKE_ATOL
    raw = np.load(RELEASE_ROOT / "smoke-raw.npy", allow_pickle=False)
    a = scorer.predict_scores({"a": raw}, endpoint_seconds=125)["a"]
    b = scorer.predict_scores({"b": raw}, endpoint_seconds=5000, half=2)["b"]
    assert a == b == scorer.readiness["smoke"]["actual_score"]
