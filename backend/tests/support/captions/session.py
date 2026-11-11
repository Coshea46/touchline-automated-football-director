"""Reusable synthetic test doubles and builders; no provider calls."""


import copy
import threading
from touchline.replay import ReplaySession, SourceRegistry
from touchline.replay.config import SessionConfig
from tests.support.replay.session import Clock, FakeBridge


class CaptionWorker:
    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append(("start",))

    def update(self, view):
        self.calls.append(("update", copy.deepcopy(view)))

    def stop(self, reason):
        self.calls.append(("stop", reason))

    def close(self):
        self.calls.append(("close", threading.get_ident()))
        return True

    @property
    def updates(self):
        return [call[1] for call in self.calls if call[0] == "update"]


async def setup(*, config=None, worker=None, registry=None):
    clock, published = Clock(), []
    worker = worker or CaptionWorker()
    session = await ReplaySession(registry or SourceRegistry.from_env({"MATCHPULSE_MATCH_COUNT": "2"}),
        config=config or SessionConfig(), bridge=FakeBridge(), caption_worker=worker,
        caption_max_audio_seconds=30, caption_max_cost_usd=.01,
        now=clock, monotonic=clock, session_id="session", publish=published.append).initialize()
    session.connect("owner")
    return session, worker, clock, published


def toggle(session, enabled=True, owner="owner"):
    return session.receive(owner, {"type": "captions", "session_id": session.session_id, "enabled": enabled})


def event(session, **values):
    view = session.caption_worker.updates[-1]
    return {"type": "caption", "session_id": session.session_id, "match_id": view["match_id"],
            "source_key": view["source_key"], "source_identity": view["source_identity"],
            "clock_epoch": session.clock_epoch, "generation": session._caption_generation,
            "provider": "elevenlabs", "model": "scribe_v2_realtime", "state": "partial",
            "text": "Synthetic provider fixture", "source_anchor_seconds": 0, **values}
