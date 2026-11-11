"""Pinned autonomous release identity; no research-checkout imports at runtime."""

import hashlib
import json
from pathlib import Path

from touchline.ml_inference.config import PACKAGE_ROOT

NEW_MODEL_VERSION = "temporal-shots3_10-d63fff64480d/classifier.pt"
LEGACY_MODEL_VERSION = "gru-stage1-02de8309/classifier.pt"
LEGACY_CHECKPOINT_SHA256 = "02de83095e5eaaf2f22978e89eec8515633ecd680ba0e9834cfb46d8cbea9319"
SELECTED_HEAD = "shots3_10"
CHECKPOINT_SHA256 = "d63fff64480d00835c14bc7c726e062b00278126d517e1e9eceb1abe0aafbd1e"
WHEEL_FILENAME = "matchpulse-0.1.0+autonomous1-py3-none-any.whl"
WHEEL_SHA256 = "3040593a8c23338d17a2d54ac5b76eed6be7df1ed6074386207f5a6010343291"
RELEASE_ROOT = PACKAGE_ROOT / "runtime" / "selected_release"
SMOKE_ATOL = 2e-5  # Published v3 temporal reproduction gate; never inferred from this run.
PARAMETER_COUNT = 4179
SOURCE_SHA256 = {
    "matchpulse.research.predictor": "2135cf5c1acb342589a9e0f56d524bcb1b6a3fc4ff6871a8e0eb630bf3df05a8",
    "matchpulse.research.core_audio": "458d0adce7d42a70107fd85286dad82c58403c1498b3393be2a2f4f673917e10",
    "matchpulse.features.schema": "0665e9a4d8a128aa4829beeba8a2c234ea681d8f7841c9d479eeb5ca42d345b8",
    "matchpulse.research.temporal": "682eb34edea7b27590d917fa651daf7a3dfee76517f6f3ae2eca966aad960e18",
    "matchpulse.research.representation": "b7f4800c08cb68d39433d200ab26bfd13927ecae96ddb274c54048b57777a73c",
    "matchpulse.features.audio": "77918bc5d4a65dc0c157ab37fb4ada6a00be71c4f57eb45ba6362e5af86ab0b1",
}
SUPPORT_SHA256 = {
    "smoke-raw.npy": "1594ba24c971d5bb39b7e56ea64a7f011e9841c0eb5ef71cb4d538e380a4beda",
    "smoke-observation.json": "17428711a0ed59bc9841eba4124203ff5a4fcf4ec4afb52716b4035540ca9098",
    "smoke-verification.json": "1f1bb6011d1d7e6fe42e0f1e5f58d5bb76e668527f8076e2ad596ababcbe5f1f",
}


def verify_release_support(root=RELEASE_ROOT):
    """Verify the real, previously published fixture before reading NumPy/pickle data."""
    root = Path(root)
    for filename, expected in SUPPORT_SHA256.items():
        path = root / filename
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError("Selected release smoke artifact missing or oversized")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Selected release smoke artifact checksum mismatch")
    observation = json.loads((root / "smoke-observation.json").read_text(encoding="utf-8"))
    verification = json.loads((root / "smoke-verification.json").read_text(encoding="utf-8"))
    if (verification["checkpoint_sha256"] != CHECKPOINT_SHA256
            or verification["head_order"] != ["any3_10", SELECTED_HEAD, "shots0_5"]):
        raise ValueError("Selected release smoke identity differs")
    return observation, verification
