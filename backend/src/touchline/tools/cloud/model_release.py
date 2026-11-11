"""Explicit selected-model release operations under the original separate $3 ledger.

Nothing executes on import. This helper never creates credentials, retries an
operation, calls another application, or stops an application/container pool.
"""

import argparse
import asyncio
import importlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from touchline.ml_inference.config import APP_NAME
from touchline.ml_inference.models.release import (
    CHECKPOINT_SHA256,
    LEGACY_CHECKPOINT_SHA256,
    LEGACY_MODEL_VERSION,
    NEW_MODEL_VERSION,
)
from touchline.ml_inference.streaming.client import _rpc
from touchline.ml_inference.streaming.contract import MAX_GPU_WORKERS, MAX_WORKER_RATE, validate_session
from touchline.paths import ARTIFACTS_DIR, INFERENCE_ARTIFACTS_DIR, PROJECT_ROOT
from touchline.config.cloud import cloud_environment
from touchline.tools.support.release_budget import CEILING_USD, ReleaseBudget
from touchline.tools.support.storage import atomic_json as _save

APPROOT = PROJECT_ROOT
ARTIFACTS = INFERENCE_ARTIFACTS_DIR / "winning-model"
MODEL_ARTIFACTS = ARTIFACTS_DIR / "model-release"
PRIVATE_CREDENTIALS = INFERENCE_ARTIFACTS_DIR / "overhaul" / "ws-credentials.json"
RESERVATIONS = {"deploy": .60, "install": .05, "equivalence": .30, "probe": .70}


def _authorize(allow_cloud):
    if not allow_cloud:
        raise ValueError("Explicit --allow-cloud authorization required")
    if APP_NAME != "matchpulse-inference":
        raise RuntimeError("Only the matchpulse-inference application is authorized")


class Budget(ReleaseBudget):
    def __init__(self, directory=None):
        super().__init__(ARTIFACTS if directory is None else directory)


def environment():
    # Shared environment loading does not instantiate any operational ledger.
    from touchline.ml_inference.deployment.packaging import runtime_artifacts

    weights = MODEL_ARTIFACTS / "yolo26s.pt"
    runtime_artifacts(weights)  # Local SHA gate before any Modal operation.
    env = cloud_environment()
    env["MATCHPULSE_ML_WEIGHTS"] = str(weights.resolve())
    return env


def _sdk(sdk):
    return sdk if sdk is not None else importlib.import_module("modal")


def _get_owned(handle, seconds):
    asynchronous = getattr(handle.get, "aio", None)
    if not callable(asynchronous):
        return handle.get(timeout=seconds)

    async def invoke():
        return await asyncio.wait_for(asynchronous(timeout=seconds), seconds + 5)

    return asyncio.run(invoke())


def _failed(budget, row, exc):
    budget.update(row, state="failed-or-unconfirmed", error_type=type(exc).__name__)


def deploy(*, allow_cloud=False, sdk=None):
    _authorize(allow_cloud)
    env = environment()
    if not PRIVATE_CREDENTIALS.is_file():
        raise ValueError("Existing private WebSocket credentials are required; this command creates none")
    private = json.loads(PRIVATE_CREDENTIALS.read_text(encoding="utf-8"))
    if not isinstance(private.get("token"), str) or not private["token"]:
        raise ValueError("Existing private WebSocket credentials are incomplete")
    budget = Budget()
    row = budget.reserve("deploy", RESERVATIONS["deploy"])
    log_path = ARTIFACTS / ("deployment-" + row["id"] + ".log")
    started = time.perf_counter()
    try:
        with log_path.open("w", encoding="utf-8") as output:
            result = subprocess.run([sys.executable, "-B", "-m", "modal", "deploy", "-m",
                                     "touchline.ml_inference.deployment.modal_app"], cwd=APPROOT, env=env,
                                    stdout=output, stderr=subprocess.STDOUT, timeout=1200, check=False)
        if result.returncode:
            raise RuntimeError("Inference deployment failed; inspect the private task log")
        worker = _sdk(sdk).Cls.from_name(APP_NAME, "ReplayWorker")()
        endpoint = _rpc(worker.web.get_web_url, timeout=15)
        parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
        if (parsed is None or parsed.scheme != "https" or not parsed.hostname
                or not parsed.hostname.endswith(".modal.run") or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("Deployed inference endpoint is unavailable or invalid")
        private["url"] = endpoint.replace("https://", "wss://", 1).rstrip("/") + "/session"
        _save(PRIVATE_CREDENTIALS, private)  # Preserve token and all existing private fields.
        record = {"app": APP_NAME, "exit_code": 0, "wall_seconds": time.perf_counter() - started,
                  "log": log_path.name, "replay_container_cap": MAX_GPU_WORKERS,
                  "min_containers": 0, "websocket_endpoint_updated": True}
        _save(ARTIFACTS / "deployment.json", record)
        # Image-build billing is not inferred from process time: retain all $.60.
        budget.update(row, state="completed", elapsed_seconds=record["wall_seconds"])
        return record
    except Exception as exc:  # noqa: BLE001 -- preserve reservation and never publish SDK credentials/URLs.
        _failed(budget, row, exc)
        raise RuntimeError("Winning-model deployment failed (" + type(exc).__name__ + "); inspect private task logs") from None


def install(*, allow_cloud=False, model="selected", verify_only=False, sdk=None):
    _authorize(allow_cloud)
    from touchline.ml_inference.models.classifiers import checkpoint_sha256
    from touchline.tools.cloud.checkpoints import install_checkpoint

    if model not in ("selected", "legacy"):
        raise ValueError("Model must be selected or legacy")
    if model == "legacy" and not verify_only:
        raise ValueError("Legacy rollback is existing immutable storage; use --verify-only")
    version, digest = ((NEW_MODEL_VERSION, CHECKPOINT_SHA256) if model == "selected" else
                       (LEGACY_MODEL_VERSION, LEGACY_CHECKPOINT_SHA256))
    path = MODEL_ARTIFACTS / version
    if checkpoint_sha256(path) != digest:
        raise ValueError("App-local immutable checkpoint SHA256 mismatch")
    budget = Budget()
    row = budget.reserve("install-" + model, RESERVATIONS["install"])
    started = time.perf_counter()
    try:
        result = install_checkpoint(path, version=version.split("/")[0], expected_sha256=digest,
                                    allow_cloud=True, verify_only=verify_only, sdk=sdk, timeout_seconds=120)
        _save(ARTIFACTS / ("checkpoint-readiness-" + model + ".json"), result)
        budget.settle(row, time.perf_counter() - started, confirmed=True)
        return {key: result[key] for key in ("installed", "readiness_verified", "checkpoint", "sha256")}
    except Exception as exc:  # noqa: BLE001 -- SDK exceptions may contain private transport information.
        _failed(budget, row, exc)
        raise RuntimeError("Winning-model checkpoint operation failed (" + type(exc).__name__ + ")") from None


def _require_readiness():
    data = json.loads((ARTIFACTS / "checkpoint-readiness-selected.json").read_text(encoding="utf-8"))
    audit = data.get("readiness", {})
    if (data.get("readiness_verified") is not True or data.get("checkpoint") != NEW_MODEL_VERSION
            or data.get("sha256") != CHECKPOINT_SHA256 or audit.get("model_version") != NEW_MODEL_VERSION
            or audit.get("bundle_sha256") != CHECKPOINT_SHA256 or audit.get("smoke", {}).get("passed") is not True):
        raise ValueError("Verified selected checkpoint readiness is required")


def _equivalence_passed(result):
    if not isinstance(result, dict) or result.get("passed") is not True:
        return False
    windows = result.get("windows")
    if not isinstance(windows, list) or len(windows) != 6 or any(not isinstance(row, dict) for row in windows):
        return False
    return ({(row.get("match_id"), row.get("observation_end")) for row in windows}
            == {(f"cam-{i}", end) for i in (1, 2) for end in (5, 7, 9)}
            and all(row.get("passed") is True and row.get("audio_contract") == "sampleclock-v3"
                    for row in windows))


def equivalence(*, allow_cloud=False, sdk=None):
    _authorize(allow_cloud)
    _require_readiness()
    spec = {"session_id": uuid.uuid4().hex, "mode": "real", "model_version": NEW_MODEL_VERSION,
            "checkpoint": NEW_MODEL_VERSION, "gpu_workers": 1, "max_duration_seconds": 30,
            "max_estimated_cost_usd": RESERVATIONS["equivalence"],
            "matches": [{"match_id": f"cam-{i}", "source_url":
                f"https://match-switcher-poc-videos.s3.eu-west-2.amazonaws.com/match-{i}.mp4",
                "offset_seconds": 120} for i in (1, 2)]}
    validate_session(spec)
    budget = Budget()
    row = budget.reserve("equivalence", RESERVATIONS["equivalence"])
    _save(ARTIFACTS / "equivalence.json", {"passed": False, "state": "pending",
                                          "model_version": NEW_MODEL_VERSION,
                                          "session_id": spec["session_id"]})
    handle = None
    started = time.perf_counter()
    returned = False
    try:
        worker = _sdk(sdk).Cls.from_name(APP_NAME, "ReplayWorker").with_options(timeout=240, max_containers=1)()
        handle = _rpc(worker.validate_sources.spawn, spec, timeout=15)
        call_id = getattr(handle, "object_id", None)
        if isinstance(call_id, str):
            budget.update(row, call_id=call_id, session_id=spec["session_id"])
        result = _get_owned(handle, 240)
        returned = True
        passed = _equivalence_passed(result)
        record = {**result, "passed": passed, "model_version": NEW_MODEL_VERSION,
                  "checkpoint": NEW_MODEL_VERSION, "session_id": spec["session_id"], "call_id": call_id}
        _save(ARTIFACTS / "equivalence.json", record)
        # The bounded method returned after its finally cleanup; even a failed
        # numerical proof is a confirmed ended call and may settle compute.
        budget.settle(row, time.perf_counter() - started, confirmed=True)
        if not passed:
            raise ValueError("Selected-source feature equivalence failed")
        return {"passed": True, "windows": len(result["windows"]), "model_version": NEW_MODEL_VERSION}
    except Exception as exc:  # noqa: BLE001 -- all diagnostics are sanitized to exception class names.
        _failed(budget, row, exc)
        if handle is not None and not returned:
            try:
                _rpc(handle.cancel, terminate_containers=False, timeout=10)
                budget.update(row, owned_cancel_requested=True)
            except Exception as cancel_exc:  # noqa: BLE001 -- cancel only this input; retain full reservation.
                budget.update(row, owned_cancel_error_type=type(cancel_exc).__name__)
        raise RuntimeError("Winning-model equivalence failed (" + type(exc).__name__ + "); no retry started") from None


def probe(*, allow_cloud=False):
    _authorize(allow_cloud)
    _require_readiness()
    proof = json.loads((ARTIFACTS / "equivalence.json").read_text(encoding="utf-8"))
    if (not _equivalence_passed(proof) or proof.get("model_version") != NEW_MODEL_VERSION
            or proof.get("checkpoint") != NEW_MODEL_VERSION):
        raise ValueError("Selected-source equivalence must pass before the seven-source probe")
    env = environment()
    if not env.get("MATCHPULSE_MODAL_WS_URL") or not env.get("MATCHPULSE_MODAL_WS_TOKEN"):
        raise ValueError("Existing authenticated inference WebSocket configuration required")
    env.update({"MATCHPULSE_MONITOR": "true", "MATCHPULSE_ML_MODE": "real",
                "MATCHPULSE_ML_CHECKPOINT": NEW_MODEL_VERSION, "MATCHPULSE_MODEL_VERSION": NEW_MODEL_VERSION,
                "MATCHPULSE_MATCH_COUNT": "7", "MATCHPULSE_GPU_WORKERS": "4",
                "MATCHPULSE_REPLAY_TRANSPORT": "websocket", "MATCHPULSE_CAPTIONS_ENABLED": "false",
                "S3_BUCKET_NAME": "match-switcher-poc-videos", "AWS_DEFAULT_REGION": "eu-west-2",
                "MATCHPULSE_SOURCE_ACCESS": "public",
                "MATCHPULSE_OFFSETS_JSON": json.dumps({f"cam-{i}": 120 for i in range(1, 8)})})
    env.pop("MATCHPULSE_MOCK_SCORES_JSON", None)
    env.pop("MATCHPULSE_SOURCES_JSON", None)
    budget = Budget()
    row = budget.reserve("probe", RESERVATIONS["probe"])
    stdout_path = ARTIFACTS / ("probe-" + row["id"] + "-stdout.log")
    stderr_path = ARTIFACTS / ("probe-" + row["id"] + "-stderr.log")
    started = time.perf_counter()
    try:
        with stdout_path.open("w", encoding="utf-8") as output, stderr_path.open("w", encoding="utf-8") as errors:
            result = subprocess.run([sys.executable, "-B", "-m", "touchline.tools.validation.replay_probe", "--allow-cloud",
                                     "--matches", "7", "--seconds", "30", "--startup-timeout", "120",
                                     "--budget", str(RESERVATIONS["probe"])], cwd=APPROOT, env=env,
                                    stdout=output, stderr=errors, timeout=210, check=False)
        if stdout_path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Probe output exceeded bounded JSON size")
        report = json.loads(stdout_path.read_text(encoding="utf-8"))
        _save(ARTIFACTS / "probe.json", report)
        confirmed = report.get("backend_metrics", {}).get("worker_stop_succeeded") is True
        budget.settle(row, time.perf_counter() - started, workers=4, confirmed=confirmed)
        if (result.returncode or report.get("passed") is not True or not confirmed
                or report.get("model_version") != NEW_MODEL_VERSION or report.get("mode") != "real"
                or report.get("score_kind") != "model" or report.get("stream_count") != 7):
            raise ValueError("Probe failed or owned worker stop was not confirmed")
        return {"passed": True, "stream_count": report.get("stream_count"),
                "latency": report.get("latency"), "worker_stop_succeeded": True,
                "html_video_playback_tested": False}
    except Exception as exc:  # noqa: BLE001 -- keep private logs and reservations, never echo raw SDK errors.
        _failed(budget, row, exc)
        raise RuntimeError("Winning-model probe failed (" + type(exc).__name__ + "); inspect private task logs") from None


def idle(*, sdk=None):
    """Read-only stats for inference pools only; never invokes or cancels work."""
    if APP_NAME != "matchpulse-inference":
        raise RuntimeError("Only matchpulse-inference may be inspected")
    sdk = _sdk(sdk)
    replay_class = sdk.Cls.from_name(APP_NAME, "ReplayWorker")
    replay = replay_class()
    validation = replay_class.with_options(timeout=240, max_containers=1)()
    functions = {"replay_run": replay.run, "replay_web": replay.web,
                 "equivalence": validation.validate_sources,
                 "legacy_features": sdk.Cls.from_name(APP_NAME, "FeatureWorker")().extract}
    classifier_class = sdk.Cls.from_name(APP_NAME, "ClassifierWorker")
    for name, version in (("selected_classifier", NEW_MODEL_VERSION), ("legacy_classifier", LEGACY_MODEL_VERSION)):
        functions[name] = classifier_class(checkpoint_name=version).ready
    pools = {}
    for name, function in functions.items():
        stats = _rpc(function.get_current_stats, timeout=5)
        pools[name] = {"runners": stats.num_total_runners,
                       "running": stats.num_running_inputs, "backlog": stats.backlog}
    record = {"app": APP_NAME, "pools": pools,
              "idle": all(not any(pool.values()) for pool in pools.values())}
    _save(ARTIFACTS / "idle.json", record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["deploy", "install", "equivalence", "probe", "idle"])
    parser.add_argument("--allow-cloud", action="store_true")
    parser.add_argument("--model", choices=["selected", "legacy"], default="selected")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if args.action != "idle" and not args.allow_cloud:
        parser.error("Explicit --allow-cloud authorization required")
    try:
        if args.action == "install":
            result = install(allow_cloud=args.allow_cloud, model=args.model, verify_only=args.verify_only)
        elif args.action == "idle":
            result = idle()
        else:
            result = globals()[args.action](allow_cloud=args.allow_cloud)
    except Exception as exc:  # noqa: BLE001 -- public CLI never prints URLs, keys or arbitrary SDK messages.
        parser.exit(1, "Winning-model operation failed (" + type(exc).__name__ + "); inspect private task artifacts.\n")
    print(json.dumps(result, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
