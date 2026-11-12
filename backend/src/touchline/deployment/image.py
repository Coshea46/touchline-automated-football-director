"""Explicit, nonsecret build inputs for the CPU backend and caption interpreter."""

from pathlib import Path


CAPTION_PYTHON = "/opt/matchpulse-captions/bin/python"
STATE_DIRECTORY = "/app/state"
ADMISSION_DATABASE = STATE_DIRECTORY + "/admission.sqlite3"


def build_backend_image(modal_module, backend_root):
    """Describe the image; only a later explicit Modal build executes commands."""
    backend_root = Path(backend_root)
    for name in ("pyproject.toml", "README.md", "requirements-captions.txt", "src"):
        if not (backend_root / name).exists():
            raise FileNotFoundError("CPU deployment requires the backend source checkout")
    return (
        modal_module.Image.debian_slim(python_version="3.12")
        .add_local_file(str(backend_root / "pyproject.toml"), "/opt/touchline-build/backend/pyproject.toml", copy=True)
        .add_local_file(str(backend_root / "README.md"), "/opt/touchline-build/backend/README.md", copy=True)
        .add_local_file(str(backend_root / "requirements-captions.txt"), "/opt/touchline-build/requirements-captions.txt", copy=True)
        .add_local_dir(str(backend_root / "src"), "/opt/touchline-build/backend/src", copy=True,
                       ignore=["**/__pycache__/**", "**/*.pyc", "**/.env", "**/.env.*", "**/artifacts/**"])
        .run_commands(
            "python -m pip wheel --no-deps --wheel-dir /opt/touchline-wheels /opt/touchline-build/backend",
            "python -m pip install --no-cache-dir /opt/touchline-wheels/touchline_backend-*.whl",
            "python -m venv /opt/matchpulse-captions",
            CAPTION_PYTHON + " -m pip install --no-cache-dir -r /opt/touchline-build/requirements-captions.txt",
            CAPTION_PYTHON + " -m pip install --no-deps /opt/touchline-wheels/touchline_backend-*.whl",
            "mkdir -p /app/backend/artifacts",
        )
        .env({"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
              "MATCHPULSE_PROJECT_ROOT": "/app", "MATCHPULSE_CAPTION_PYTHON": CAPTION_PYTHON,
              "MATCHPULSE_MONITOR": "false", "MATCHPULSE_CAPTIONS_ENABLED": "false"})
        .workdir("/app")
    )
