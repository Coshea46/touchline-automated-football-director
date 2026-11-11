"""Validated replay configuration and environment parsing."""

import json
import os
import re
from dataclasses import dataclass
from touchline.replay.director import DIRECTOR_MODES
from touchline.replay.sources import number
from touchline.ml_inference.models.release import NEW_MODEL_VERSION
from touchline.ml_inference.streaming.contract import MAX_GPU_WORKERS, MAX_SESSION_BUDGET_USD


@dataclass(frozen=True)
class SessionConfig:
    monitor: bool = False
    mode: str = "disabled"
    model_version: str = "disabled"
    checkpoint: str | None = None
    max_duration_seconds: float = 300
    max_estimated_cost_usd: float = 1
    heartbeat_seconds: float = 5
    pause_idle_seconds: float = 10
    poll_seconds: float = 0.1
    control_seconds: float = 0.25
    max_restarts: int = 2
    mock_scores: dict | None = None
    gpu_workers: int | None = None
    shutdown_rpc_seconds: float = 5
    replay_transport: str = "websocket"
    director_mode: str = "balanced"
    director_emergency_enabled: bool = False
    director_emergency_margin: float = 0.50

    def __post_init__(self):
        if not isinstance(self.monitor, bool):
            raise TypeError("monitor must be an explicit boolean")
        if self.monitor and self.mode not in ("real", "features_mock"):
            raise ValueError("Monitoring requires explicit MATCHPULSE_ML_MODE=real or features_mock")
        if not self.monitor and self.mode != "disabled":
            raise ValueError("Disabled monitoring must use disabled mode")
        if self.monitor and self.mode == "real" and not self.checkpoint:
            raise ValueError("Real monitoring requires MATCHPULSE_ML_CHECKPOINT")
        if (not isinstance(self.model_version, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", self.model_version)):
            raise ValueError("Invalid model version")
        number(self.max_duration_seconds, "max_duration_seconds", minimum=10, maximum=3600)
        number(self.max_estimated_cost_usd, "max_estimated_cost_usd", minimum=0.01, maximum=MAX_SESSION_BUDGET_USD)
        number(self.heartbeat_seconds, "heartbeat_seconds", minimum=1, maximum=30)
        number(self.pause_idle_seconds, "pause_idle_seconds", minimum=1, maximum=30)
        number(self.poll_seconds, "poll_seconds", minimum=0.05, maximum=2)
        number(self.control_seconds, "control_seconds", minimum=0.1, maximum=2)
        number(self.shutdown_rpc_seconds, "shutdown_rpc_seconds", minimum=0.01, maximum=30)
        if not isinstance(self.max_restarts, int) or isinstance(self.max_restarts, bool) or not 0 <= self.max_restarts <= 3:
            raise ValueError("max_restarts must be between 0 and 3")
        if self.mock_scores is not None:
            if self.mode != "features_mock" or not isinstance(self.mock_scores, dict):
                raise ValueError("mock_scores is permitted only in explicit features_mock mode")
            for match_id, score in self.mock_scores.items():
                if not isinstance(match_id, str):
                    raise TypeError("Mock score keys must be match IDs")
                number(score, "mock_score", maximum=1)
        if self.gpu_workers is not None and (type(self.gpu_workers) is not int or not 1 <= self.gpu_workers <= MAX_GPU_WORKERS):
            raise ValueError(f"gpu_workers must be between 1 and {MAX_GPU_WORKERS}")
        if self.replay_transport not in ("websocket", "dict"):
            raise ValueError("MATCHPULSE_REPLAY_TRANSPORT must be websocket or dict")
        if not isinstance(self.director_mode, str) or self.director_mode not in DIRECTOR_MODES:
            raise ValueError("MATCHPULSE_DIRECTOR_MODE must be relaxed, balanced or all_action")
        if type(self.director_emergency_enabled) is not bool:
            raise TypeError("director_emergency_enabled must be boolean")
        number(self.director_emergency_margin, "director_emergency_margin", minimum=0.05, maximum=1)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        flag = env.get("MATCHPULSE_MONITOR", "false").lower()
        if flag not in ("true", "false"):
            raise ValueError("MATCHPULSE_MONITOR must be true or false")
        enabled = flag == "true"
        mode = (env.get("MATCHPULSE_ML_MODE") or "real") if enabled else "disabled"
        checkpoint = (env.get("MATCHPULSE_ML_CHECKPOINT") or NEW_MODEL_VERSION) if mode == "real" else None
        version = env.get("MATCHPULSE_MODEL_VERSION") or (checkpoint if mode == "real" else
                                                        "features-mock-v1" if enabled else "disabled")
        raw_mock = env.get("MATCHPULSE_MOCK_SCORES_JSON")
        emergency_flag = env.get("MATCHPULSE_DIRECTOR_EMERGENCY_ENABLED", "false").lower()
        if emergency_flag not in ("true", "false"):
            raise ValueError("MATCHPULSE_DIRECTOR_EMERGENCY_ENABLED must be true or false")
        try:
            mock_scores = json.loads(raw_mock) if raw_mock and enabled else None
        except ValueError as exc:
            raise ValueError("Invalid MATCHPULSE_MOCK_SCORES_JSON") from exc
        return cls(monitor=enabled, mode=mode, model_version=version, checkpoint=checkpoint,
                   max_duration_seconds=float(env.get("MATCHPULSE_SESSION_SECONDS", "300")),
                   max_estimated_cost_usd=float(env.get("MATCHPULSE_SESSION_BUDGET_USD", "1")),
                   mock_scores=mock_scores,
                   gpu_workers=int(env["MATCHPULSE_GPU_WORKERS"]) if env.get("MATCHPULSE_GPU_WORKERS") else None,
                   replay_transport=env.get("MATCHPULSE_REPLAY_TRANSPORT") or "websocket",
                   director_mode=env.get("MATCHPULSE_DIRECTOR_MODE") or "balanced",
                   director_emergency_enabled=emergency_flag == "true",
                   director_emergency_margin=float(env.get("MATCHPULSE_DIRECTOR_EMERGENCY_MARGIN", "0.50")))
