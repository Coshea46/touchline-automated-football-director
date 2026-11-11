"""Client configuration, compatible with the application's Python 3.10."""

import math
import os
from dataclasses import dataclass
from numbers import Real
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent

APP_NAME = "matchpulse-inference"
MODEL_VOLUME = "matchpulse-inference-models"
SCHEMA_VERSION = "matchpulse-215-v1"


def finite_number(value, name, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a finite number")
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return float(value)


@dataclass(frozen=True)
class InferenceConfig:
    # There is deliberately no implicit mock or production mode.
    mode: str
    staying_bonus: float = 0.10
    gpu: str = "T4"
    worker_concurrency: int = 1  # container cap, not concurrent inputs per container
    max_inflight: int = 1
    worker_timeout_seconds: int = 180
    call_timeout_seconds: float = 240
    request_timeout_seconds: float = 300
    classifier_timeout_seconds: int = 60
    scaledown_seconds: int = 5
    fp16: bool = True
    max_window_bytes: int = 512 * 1024 * 1024
    checkpoint: str | None = None  # relative to the independent inference model Volume

    def __post_init__(self):
        if self.mode not in ("mock", "features_mock", "real"):
            raise ValueError("mode must be explicitly mock, features_mock, or real")
        finite_number(self.staying_bonus, "staying_bonus")
        for name in ("worker_concurrency", "max_inflight", "worker_timeout_seconds",
                     "classifier_timeout_seconds", "scaledown_seconds", "max_window_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("call_timeout_seconds", "request_timeout_seconds"):
            if finite_number(getattr(self, name), name) == 0:
                raise ValueError(f"{name} must be positive")
        if not isinstance(self.gpu, str) or not self.gpu.strip():
            raise ValueError("gpu must be a nonempty Modal GPU type")
        if not isinstance(self.fp16, bool):
            raise TypeError("fp16 must be boolean")
        if self.checkpoint is not None:
            from pathlib import PurePosixPath

            path = PurePosixPath(self.checkpoint)
            if (not self.checkpoint or "\\" in self.checkpoint or ":" in self.checkpoint
                    or path.is_absolute() or ".." in path.parts or path == PurePosixPath(".")):
                raise ValueError("checkpoint must be a safe relative path in the inference model Volume")
        if self.mode == "real" and self.checkpoint is None:
            raise ValueError("Real mode requires MATCHPULSE_ML_CHECKPOINT; no mock fallback exists")

    @classmethod
    def from_env(cls):
        mode = os.environ.get("MATCHPULSE_ML_MODE")
        if not mode:
            raise ValueError("Set MATCHPULSE_ML_MODE explicitly: mock, features_mock, or real")
        values = {"mode": mode, "checkpoint": os.environ.get("MATCHPULSE_ML_CHECKPOINT")}
        types = {
            "staying_bonus": float, "gpu": str, "worker_concurrency": int,
            "max_inflight": int, "worker_timeout_seconds": int,
            "call_timeout_seconds": float, "request_timeout_seconds": float,
            "classifier_timeout_seconds": int, "scaledown_seconds": int,
            "max_window_bytes": int,
        }
        for field, parser in types.items():
            key = "MATCHPULSE_ML_" + field.upper()
            if key in os.environ:
                try:
                    values[field] = parser(os.environ[key])
                except ValueError as exc:
                    raise ValueError(f"Invalid {key}") from exc
        if "MATCHPULSE_ML_FP16" in os.environ:
            value = os.environ["MATCHPULSE_ML_FP16"].lower()
            if value not in ("true", "false"):
                raise ValueError("MATCHPULSE_ML_FP16 must be true or false")
            values["fp16"] = value == "true"
        return cls(**values)
