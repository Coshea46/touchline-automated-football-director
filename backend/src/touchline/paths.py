"""Writable application paths, independent of the Python package installation."""

import os
from pathlib import Path


def _project_root():
    configured = os.environ.get("MATCHPULSE_PROJECT_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    # Editable checkouts keep their configuration at the monorepo root. A wheel
    # installation instead uses its working directory, or an explicit override.
    for parent in Path(__file__).resolve().parents:
        if (parent / "backend" / "pyproject.toml").is_file() and (parent / "frontend").is_dir():
            return parent
    return Path.cwd().resolve()


PROJECT_ROOT = _project_root()
BACKEND_ROOT = PROJECT_ROOT / "backend"
ARTIFACTS_DIR = BACKEND_ROOT / "artifacts"
INFERENCE_ARTIFACTS_DIR = ARTIFACTS_DIR / "inference"
