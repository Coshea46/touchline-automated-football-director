"""Shared subprocess configuration, without importing operational commands."""

import json
import os
from pathlib import Path

from touchline.paths import INFERENCE_ARTIFACTS_DIR


def cloud_environment(*, credentials_path=None):
    """Copy settings and optional local credentials; never contact a provider.

    Detector weights must be supplied explicitly through MATCHPULSE_ML_WEIGHTS.
    The application does not infer or read a sibling research checkout.
    """
    env = dict(os.environ)
    env.update({"MATCHPULSE_ML_GPU": "T4", "MATCHPULSE_ML_WORKER_CONCURRENCY": "1",
                "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
    credentials = (Path(credentials_path) if credentials_path is not None else
                   INFERENCE_ARTIFACTS_DIR / "overhaul" / "ws-credentials.json")
    if credentials.exists():
        private = json.loads(credentials.read_text(encoding="utf-8"))
        env["MATCHPULSE_REPLAY_TRANSPORT"] = "websocket"
        env["MATCHPULSE_MODAL_WS_TOKEN"] = private["token"]
        if private.get("url"):
            env["MATCHPULSE_MODAL_WS_URL"] = private["url"]
    return env


def window_environment():
    """Retain the historical complete-window validation worker settings."""
    env = dict(os.environ)
    env.update({
        "MATCHPULSE_ML_MODE": "features_mock", "MATCHPULSE_ML_GPU": "T4",
        "MATCHPULSE_ML_WORKER_CONCURRENCY": "1", "MATCHPULSE_ML_MAX_INFLIGHT": "1",
        "MATCHPULSE_ML_SCALEDOWN_SECONDS": "5", "MATCHPULSE_ML_WORKER_TIMEOUT_SECONDS": "180",
        "MATCHPULSE_ML_CALL_TIMEOUT_SECONDS": "300", "MATCHPULSE_ML_REQUEST_TIMEOUT_SECONDS": "600",
        "PYTHONIOENCODING": "utf-8",
    })
    return env
