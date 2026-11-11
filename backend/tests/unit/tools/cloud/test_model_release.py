"""Offline tests for unit tools cloud test_model_release."""


import json
import shutil
import subprocess
from types import SimpleNamespace
import pytest
from touchline.tools.cloud import model_release as winning
from touchline.ml_inference.models.release import CHECKPOINT_SHA256, NEW_MODEL_VERSION
from tests.paths import ML_FIXTURES


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(winning, "ARTIFACTS", tmp_path / "winning-model")
    monkeypatch.setattr(winning, "MODEL_ARTIFACTS", tmp_path / "model-release")
    monkeypatch.setattr(winning, "PRIVATE_CREDENTIALS", tmp_path / "old-overhaul" / "ws-credentials.json")
    winning.PRIVATE_CREDENTIALS.parent.mkdir()
    winning.PRIVATE_CREDENTIALS.write_text(json.dumps({"token": "PRIVATE_TEST_TOKEN", "url": "wss://old.modal.run/session"}))
    checkpoint = winning.MODEL_ARTIFACTS / NEW_MODEL_VERSION
    checkpoint.parent.mkdir(parents=True)
    shutil.copyfile(ML_FIXTURES / "selected_release/classifier.pt", checkpoint)
    monkeypatch.setattr(winning, "environment", lambda: {
        "MATCHPULSE_MODAL_WS_URL": "wss://own.modal.run/session",
        "MATCHPULSE_MODAL_WS_TOKEN": "PRIVATE_TEST_TOKEN",
        "MATCHPULSE_ML_WEIGHTS": str(winning.MODEL_ARTIFACTS / "yolo26s.pt"),
    })
    old = tmp_path / "old-overhaul" / "budget.json"
    old.write_text('{"ceiling_usd":10,"sentinel":"unchanged"}')
    return SimpleNamespace(root=tmp_path, old_budget=old)


def readiness():
    winning._save(winning.ARTIFACTS / "checkpoint-readiness-selected.json", {
        "readiness_verified": True, "checkpoint": NEW_MODEL_VERSION, "sha256": CHECKPOINT_SHA256,
        "readiness": {"model_version": NEW_MODEL_VERSION, "bundle_sha256": CHECKPOINT_SHA256,
                      "smoke": {"passed": True}},
    })


def proof():
    return {"passed": True, "model_version": NEW_MODEL_VERSION, "checkpoint": NEW_MODEL_VERSION, "windows": [
        {"match_id": f"cam-{i}", "observation_end": end, "passed": True, "audio_contract": "sampleclock-v3"}
        for i in (1, 2) for end in (5, 7, 9)
    ]}


def sdk_fixture(result=None, failure=None):
    lookups, options, instances, spawns, cancels, gets = [], [], [], [], [], []
    stats = SimpleNamespace(num_total_runners=0, num_running_inputs=0, backlog=0)
    handle = SimpleNamespace(object_id="fc-owned-only")

    def get(*, timeout):
        gets.append(timeout)
        if failure:
            raise failure
        return result if result is not None else proof()

    handle.get = get
    handle.cancel = lambda **kwargs: cancels.append(kwargs)

    def spawn(spec):
        spawns.append(spec)
        return handle

    method = SimpleNamespace(get_current_stats=lambda: stats)
    web = SimpleNamespace(get_web_url=lambda: "https://own.modal.run", get_current_stats=lambda: stats)
    validation = SimpleNamespace(spawn=spawn, get_current_stats=lambda: stats)
    worker = SimpleNamespace(run=method, web=web, validate_sources=validation, ready=method, extract=method)

    class Cls:
        def __call__(self, **kwargs):
            instances.append(kwargs)
            return worker

        def with_options(self, **kwargs):
            options.append(kwargs)
            return self

        @staticmethod
        def from_name(app, name):
            lookups.append((app, name))
            return Cls()

    return SimpleNamespace(sdk=SimpleNamespace(Cls=Cls), lookups=lookups, options=options,
                           instances=instances, spawns=spawns, cancels=cancels, gets=gets, stats=stats)


@pytest.mark.parametrize("action", ["deploy", "install", "equivalence", "probe"])
def test_cloud_actions_require_opt_in_before_any_sdk_or_ledger_access(workspace, action):
    with pytest.raises(ValueError, match="allow-cloud"):
        getattr(winning, action)()
    assert not winning.ARTIFACTS.exists()


def test_separate_budget_persists_conservatively_and_does_not_touch_old_ledgers(workspace):
    first, stale = winning.Budget(), winning.Budget()
    row = first.reserve("one", 2.9)
    with pytest.raises(RuntimeError, match=r"\$3"):
        stale.reserve("two", .2)
    first.settle(row, 1, confirmed=False)
    assert winning.Budget().data["estimated_usd"] == 2.9
    first.settle(row, 1, confirmed=True)
    assert winning.Budget().data["estimated_usd"] < 2.9
    assert json.loads(workspace.old_budget.read_text())["sentinel"] == "unchanged"
    assert winning.Budget().data["ceiling_usd"] == 3


@pytest.mark.parametrize("amount", [0, -1, True, float("nan"), float("inf")])
def test_invalid_reservations_fail_before_cloud(workspace, amount):
    with pytest.raises(ValueError):
        winning.Budget().reserve("invalid", amount)


def test_budget_lock_fails_closed_and_wrong_task_ledger_is_rejected(workspace):
    winning.ARTIFACTS.mkdir()
    (winning.ARTIFACTS / "budget.lock").write_text("another process")
    with pytest.raises(RuntimeError, match="locked"):
        winning.Budget().reserve("blocked", .1)
    (winning.ARTIFACTS / "budget.lock").unlink()
    winning._save(winning.ARTIFACTS / "budget.json", {"task": "old", "ceiling_usd": 10})
    with pytest.raises(RuntimeError, match="Unexpected"):
        winning.Budget()


def test_real_environment_replaces_research_weights_before_sdk(monkeypatch, tmp_path):
    from touchline.ml_inference.deployment import packaging

    monkeypatch.setattr(winning, "MODEL_ARTIFACTS", tmp_path)
    monkeypatch.setattr(winning, "cloud_environment", lambda: {"MATCHPULSE_ML_WEIGHTS": "DO_NOT_FORWARD_RESEARCH"})
    verified = []
    monkeypatch.setattr(packaging, "runtime_artifacts", lambda path: verified.append(path))
    env = winning.environment()
    assert verified == [tmp_path / "yolo26s.pt"]
    assert env["MATCHPULSE_ML_WEIGHTS"] == str((tmp_path / "yolo26s.pt").resolve())


def test_deploy_targets_only_inference_with_bounded_subprocess_and_preserves_token(workspace, monkeypatch):
    sdk = sdk_fixture()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["stdout"].write("private build log")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(winning.subprocess, "run", run)
    record = winning.deploy(allow_cloud=True, sdk=sdk.sdk)
    command, options = calls[0]
    assert command[-3:] == ["deploy", "-m", "touchline.ml_inference.deployment.modal_app"]
    assert options["timeout"] == 1200 and options["cwd"] == winning.APPROOT
    assert sdk.lookups == [("matchpulse-inference", "ReplayWorker")]
    assert record["websocket_endpoint_updated"]
    private = json.loads(winning.PRIVATE_CREDENTIALS.read_text())
    assert private == {"token": "PRIVATE_TEST_TOKEN", "url": "wss://own.modal.run/session"}
    assert winning.Budget().data["estimated_usd"] == .60
    assert "PRIVATE_TEST_TOKEN" not in json.dumps(record)


def test_deployment_timeout_reserves_full_amount_without_retry(workspace, monkeypatch):
    attempts = []

    def run(command, **kwargs):
        attempts.append(command)
        raise subprocess.TimeoutExpired(command, 1200)

    monkeypatch.setattr(winning.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="TimeoutExpired"):
        winning.deploy(allow_cloud=True, sdk=object())
    assert len(attempts) == 1
    assert winning.Budget().data["estimated_usd"] == .60
    assert winning.Budget().data["entries"][0]["state"] == "failed-or-unconfirmed"


def test_selected_install_uses_local_immutable_bytes_and_no_research_path(workspace, monkeypatch):
    from touchline.tools.cloud import checkpoints as installer

    calls = []

    def install(path, **kwargs):
        calls.append((path, kwargs))
        return {"installed": True, "readiness_verified": True, "checkpoint": NEW_MODEL_VERSION,
                "sha256": CHECKPOINT_SHA256, "readiness": {"model_version": NEW_MODEL_VERSION}}

    monkeypatch.setattr(installer, "install_checkpoint", install)
    result = winning.install(allow_cloud=True, sdk="fake-sdk")
    path, kwargs = calls[0]
    assert path == winning.MODEL_ARTIFACTS / NEW_MODEL_VERSION
    assert kwargs == {"version": NEW_MODEL_VERSION.split("/")[0], "expected_sha256": CHECKPOINT_SHA256,
                      "allow_cloud": True, "verify_only": False, "sdk": "fake-sdk", "timeout_seconds": 120}
    assert result["readiness_verified"]
    with pytest.raises(ValueError, match="verify-only"):
        winning.install(allow_cloud=True, model="legacy", sdk=object())


def test_equivalence_uses_two_real_sources_and_only_one_owned_gpu_call(workspace):
    readiness()
    sdk = sdk_fixture()
    assert winning.equivalence(allow_cloud=True, sdk=sdk.sdk)["passed"]
    assert sdk.lookups == [("matchpulse-inference", "ReplayWorker")]
    assert sdk.options == [{"timeout": 240, "max_containers": 1}]
    assert sdk.gets == [240] and not sdk.cancels
    spec = sdk.spawns[0]
    assert spec["mode"] == "real" and spec["gpu_workers"] == 1
    assert spec["checkpoint"] == spec["model_version"] == NEW_MODEL_VERSION
    assert [(s["match_id"], s["offset_seconds"]) for s in spec["matches"]] == [("cam-1", 120), ("cam-2", 120)]
    row = winning.Budget().data["entries"][0]
    assert row["call_id"] == "fc-owned-only" and row["cleanup_confirmed"]


def test_equivalence_timeout_cancels_only_owned_input_without_container_termination(workspace):
    readiness()
    sdk = sdk_fixture(failure=TimeoutError("sensitive ignored"))
    with pytest.raises(RuntimeError, match="TimeoutError"):
        winning.equivalence(allow_cloud=True, sdk=sdk.sdk)
    assert len(sdk.spawns) == 1 and sdk.gets == [240]
    assert sdk.cancels == [{"terminate_containers": False}]
    assert winning.Budget().data["estimated_usd"] == .30


def test_incomplete_equivalence_cannot_leave_a_passing_receipt_for_probe(workspace):
    readiness()
    sdk = sdk_fixture(result={"passed": True, "windows": []})
    with pytest.raises(RuntimeError):
        winning.equivalence(allow_cloud=True, sdk=sdk.sdk)
    assert json.loads((winning.ARTIFACTS / "equivalence.json").read_text())["passed"] is False
    assert not sdk.cancels  # Input already returned; no unrelated cancellation is necessary.
    with pytest.raises(ValueError, match="equivalence"):
        winning.probe(allow_cloud=True)


@pytest.mark.parametrize("confirmed", [True, False])
def test_probe_is_one_bounded_real_seven_source_process_and_settles_only_actual_stop(workspace, monkeypatch, confirmed):
    readiness()
    winning._save(winning.ARTIFACTS / "equivalence.json", proof())
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["stdout"].write(json.dumps({"passed": True, "stream_count": 7,
            "model_version": NEW_MODEL_VERSION, "mode": "real", "score_kind": "model",
            "owned_worker_cancelled": True,  # Old success flag alone must never settle.
            "backend_metrics": {"worker_stop_succeeded": confirmed}}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(winning.subprocess, "run", run)
    if confirmed:
        assert winning.probe(allow_cloud=True)["worker_stop_succeeded"]
    else:
        with pytest.raises(RuntimeError):
            winning.probe(allow_cloud=True)
    assert len(calls) == 1
    command, options = calls[0]
    assert command[3:5] == ["touchline.tools.validation.replay_probe", "--allow-cloud"]
    assert command[-8:] == ["--matches", "7", "--seconds", "30", "--startup-timeout", "120", "--budget", "0.7"]
    assert options["timeout"] == 210 and options["cwd"] == winning.APPROOT
    env = options["env"]
    assert env["MATCHPULSE_ML_CHECKPOINT"] == env["MATCHPULSE_MODEL_VERSION"] == NEW_MODEL_VERSION
    assert env["MATCHPULSE_MONITOR"] == "true" and env["MATCHPULSE_ML_MODE"] == "real"
    assert env["MATCHPULSE_CAPTIONS_ENABLED"] == "false" and env["MATCHPULSE_GPU_WORKERS"] == "4"
    assert json.loads(env["MATCHPULSE_OFFSETS_JSON"]) == {f"cam-{i}": 120 for i in range(1, 8)}
    row = winning.Budget().data["entries"][0]
    assert row["reserved_usd"] == .70
    assert (row["estimated_usd"] < .70) is confirmed


def test_idle_uses_only_owned_inference_stats_without_spawning_or_cancelling(workspace):
    sdk = sdk_fixture()
    result = winning.idle(sdk=sdk.sdk)
    assert result["idle"] and len(result["pools"]) == 6
    assert all(app == "matchpulse-inference" for app, _ in sdk.lookups)
    assert not sdk.spawns and not sdk.cancels and not winning.Budget().data["entries"]
    sdk.stats.num_running_inputs = 1
    assert not winning.idle(sdk=sdk.sdk)["idle"]


def test_owned_handle_uses_public_async_get_with_sdk_and_rpc_deadlines():
    calls = []

    def synchronous(**kwargs):
        pytest.fail("The production .aio path must be used")

    async def asynchronous(**kwargs):
        calls.append(kwargs)
        return {"returned": True}

    synchronous.aio = asynchronous
    assert winning._get_owned(SimpleNamespace(get=synchronous), 240) == {"returned": True}
    assert calls == [{"timeout": 240}]
