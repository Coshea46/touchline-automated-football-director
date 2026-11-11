"""Explicit trusted-checkpoint upload and CPU readiness; never starts a GPU."""

import argparse
import hashlib
import importlib
import io
import json
import logging
import re
from pathlib import Path

from touchline.ml_inference.models.classifiers import MAX_CHECKPOINT_BYTES
from touchline.ml_inference.config import APP_NAME, MODEL_VOLUME, SCHEMA_VERSION
from touchline.ml_inference.models.release import (
    CHECKPOINT_SHA256,
    NEW_MODEL_VERSION,
    PARAMETER_COUNT,
    SELECTED_HEAD,
    SMOKE_ATOL,
    SOURCE_SHA256,
)

logger = logging.getLogger(__name__)


def install_checkpoint(trusted_checkpoint, *, version, expected_sha256, allow_cloud=False,
                       sdk=None, verify_only=False, timeout_seconds=120):
    if not allow_cloud:
        raise ValueError("Checkpoint installation requires explicit --allow-cloud")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", version):
        raise ValueError("Version must be one safe, immutable directory name")
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("The trusted export's explicit SHA256 is required")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not 1 <= timeout_seconds <= 180):
        raise ValueError("CPU readiness timeout must be bounded to 1..180 seconds")
    path = Path(trusted_checkpoint).expanduser().resolve(strict=True)
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_CHECKPOINT_BYTES:
        raise ValueError("Trusted portable checkpoint must be a nonempty file <=64 MiB")
    with path.open("rb") as stream:
        payload = stream.read(MAX_CHECKPOINT_BYTES + 1)
    if not payload or len(payload) > MAX_CHECKPOINT_BYTES:
        raise ValueError("Trusted portable checkpoint must be a nonempty file <=64 MiB")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected_sha256.lower():
        raise ValueError("Trusted checkpoint SHA256 mismatch; nothing uploaded")
    selected = f"{version}/classifier.pt" == NEW_MODEL_VERSION
    if selected and digest != CHECKPOINT_SHA256:
        raise ValueError("Selected release requires its immutable checkpoint SHA256")
    sdk = sdk or importlib.import_module("modal")
    remote = f"{version}/classifier.pt"
    if not verify_only:
        volume = sdk.Volume.from_name(MODEL_VOLUME, create_if_missing=True)
        # Upload the verified immutable bytes, not a path that may change later.
        # The SDK rejects an existing target; there is deliberately no force flag.
        with volume.batch_upload(force=False) as batch:
            batch.put_file(io.BytesIO(payload), "/" + remote)
    worker = sdk.Cls.from_name(APP_NAME, "ClassifierWorker")(checkpoint_name=remote)
    handle = worker.ready.spawn()
    try:
        readiness = handle.get(timeout=timeout_seconds)
    except Exception:
        # Cancel this CPU readiness input only, never another invocation/app.
        try:
            handle.cancel(terminate_containers=False)
        except Exception as exc:  # noqa: BLE001 - preserve initial failure and omit sensitive SDK details
            logger.warning("Owned CPU readiness cancellation failed (%s)", type(exc).__name__)
        raise
    if (not isinstance(readiness, dict) or readiness.get("bundle_sha256") != digest
            or readiness.get("model_version") != remote or readiness.get("schema") != SCHEMA_VERSION
            or readiness.get("input_shape") != [75, 215] or readiness.get("audio_required") is not True):
        raise ValueError("CPU readiness failed bundle/schema/version audit; deploy the current inference app")
    architecture = readiness.get("architecture", {})
    if selected:
        smoke = readiness.get("smoke", {})
        if (architecture.get("kind") != "temporal_gru" or architecture.get("input_size") != 68
                or architecture.get("hidden_size") != 16 or architecture.get("heads") != 3
                or architecture.get("parameter_count") != PARAMETER_COUNT
                or readiness.get("selected_head") != SELECTED_HEAD
                or readiness.get("audio_contract") != "sampleclock-v3"
                or readiness.get("source_sha256") != SOURCE_SHA256 or smoke.get("passed") is not True
                or smoke.get("atol") != SMOKE_ATOL or smoke.get("rtol") != 0
                or not isinstance(smoke.get("absolute_difference"), (int, float))
                or not 0 <= smoke["absolute_difference"] <= SMOKE_ATOL):
            raise ValueError("CPU readiness failed selected release source/head/fixture audit")
    elif architecture.get("kind") != "gru" or architecture.get("hidden_size") != 64:
        raise ValueError("CPU readiness failed legacy architecture audit")
    return {"installed": not verify_only, "readiness_verified": True,
            "volume": MODEL_VOLUME, "checkpoint": remote, "sha256": digest, "readiness": readiness}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-cloud", action="store_true")
    parser.add_argument("--trusted-checkpoint", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--verify-only", action="store_true",
                        help="Verify an existing immutable version without attempting another upload")
    args = parser.parse_args()
    if not args.allow_cloud:
        parser.error("Checkpoint installation requires explicit --allow-cloud")
    try:
        result = install_checkpoint(args.trusted_checkpoint, version=args.version,
                                    expected_sha256=args.sha256, allow_cloud=True,
                                    verify_only=args.verify_only)
    except Exception as exc:  # noqa: BLE001 - SDK exceptions may expose signed transport/authentication details
        parser.exit(1, "Checkpoint installation/readiness failed (" + type(exc).__name__ + ").\n")
    print(json.dumps(result, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
