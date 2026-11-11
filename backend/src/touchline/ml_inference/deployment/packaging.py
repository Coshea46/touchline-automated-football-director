"""Build-time verification only; the deployed runtime has no sibling-path imports."""

import hashlib
import json
from pathlib import Path

from touchline.ml_inference.models.release import WHEEL_FILENAME, WHEEL_SHA256, verify_release_support
from touchline.ml_inference.config import PACKAGE_ROOT


def runtime_artifacts(weights_path):
    root = PACKAGE_ROOT
    provenance = json.loads((root / "runtime/provenance.json").read_text(encoding="utf-8"))
    wheel = root / "runtime" / provenance["wheel_filename"]
    if provenance["wheel_filename"] != WHEEL_FILENAME or provenance["wheel_sha256"] != WHEEL_SHA256:
        raise ValueError("Runtime wheel must match the audited autonomous release")
    if not weights_path:
        raise ValueError("Set MATCHPULSE_ML_WEIGHTS to an existing local YOLO26s file before deployment")
    weights = Path(weights_path).expanduser().resolve(strict=True)
    for path, digest in [(wheel, provenance["wheel_sha256"]), (weights, provenance["yolo_sha256"])]:
        if not path.is_file():
            raise FileNotFoundError(f"Required runtime artifact missing: {path.name}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            raise ValueError(f"Runtime artifact checksum mismatch: {path.name}")
    verify_release_support()
    return wheel, weights
