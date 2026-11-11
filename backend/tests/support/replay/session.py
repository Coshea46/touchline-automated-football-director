"""Reusable synthetic test doubles and builders; no provider calls."""


import threading
from touchline.replay import Director, MatchSource, SourceRegistry
from touchline.replay.config import SessionConfig


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.snapshot = None
        self.error = None

    def start(self, spec):
        self.calls.append(("start", spec, threading.get_ident()))
        if self.error:
            raise self.error
        return "own-handle"

    def update_control(self, session_id, control):
        self.calls.append(("control", control, threading.get_ident()))

    def latest(self, session_id):
        self.calls.append(("latest", session_id, threading.get_ident()))
        return self.snapshot

    def stop(self, session_id, handle):
        self.calls.append(("stop", handle, threading.get_ident()))


def registry(count=2):
    return SourceRegistry([MatchSource(f"cam-{i}", f"Match {i}", offset_seconds=i * 10,
                                       key=f"match-{i}.mp4") for i in range(1, count + 1)])


def configuration(**kwargs):
    # These infrastructure fixtures use the immediate-confirmation mode. Mode
    # policy/default tests exercise Balanced independently below.
    kwargs.setdefault("director_mode", "all_action")
    return SessionConfig(monitor=True, mode="features_mock", model_version="mock-v1", **kwargs)


def scores(values=None, *, end=5, sequence=1, now=1000, epoch=0):
    values = values or {"cam-1": 0.4, "cam-2": 0.8}
    return {"session_id": "session", "model_version": "mock-v1", "clock_epoch": epoch,
            "worker_sequence": sequence, "score_kind": "mock", "state": "running",
            "health": {match_id: "healthy" for match_id in values},
            "scores": [{"match_id": match_id, "session_id": "session", "model_version": "mock-v1",
                        "clock_epoch": epoch, "sequence": sequence, "score": value,
                        "observation_start": end - 5, "observation_end": end,
                        "produced_at": now, "latency_seconds": 0.2, "status": "healthy"}
                       for match_id, value in values.items()]}


def director(**kwargs):
    kwargs.setdefault("staying_bonus", 0.10)
    kwargs.setdefault("minimum_switch_seconds", 2)
    kwargs.setdefault("confirmations_required", 1)
    return Director(["cam-1", "cam-2"], session_id="session", model_version="mock-v1",
                    mode="features_mock", **kwargs)


def playback(session, **kwargs):
    return {"type": "playback", "session_id": session.session_id,
            "match_id": session.director.current_match_id, "position": 0,
            "playing": True, "rate": 1, "stalled": False, **kwargs}
