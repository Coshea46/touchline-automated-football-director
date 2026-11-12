"""Offline visitor transport and admission doubles."""

import asyncio

from touchline.hosted.admission import AdmissionDenied
from touchline.ml_inference.models.release import NEW_MODEL_VERSION
from touchline.replay import MatchSource, ReplaySession, SessionConfig, SourceRegistry
from tests.support.captions.session import CaptionWorker
from tests.support.replay.session import Clock, FakeBridge


class Admission:
    def __init__(self):
        self.creations, self.connections, self.messages, self.http = [], [], [], []
        self.acquired, self.released = [], []
        self.denial = None
        self.closed = False

    async def admit_new(self, peer):
        self.creations.append(peer)
        return "hashed-" + peer

    async def allow_connection(self, peer):
        self.connections.append(peer)

    async def allow_message(self, session_id):
        self.messages.append(session_id)

    async def allow_http(self, peer):
        self.http.append(peer)

    async def acquire_ai(self, session_id, peer):
        if self.denial:
            raise AdmissionDenied(self.denial, 12)
        self.acquired.append((session_id, peer))
        return {"duration_seconds": 120, "reserved_usd": .70}

    async def release_ai(self, session_id, confirmed=False):
        self.released.append((session_id, confirmed))

    async def close(self):
        self.closed = True


class Connection:
    def __init__(self):
        self.payloads, self.closed = [], []
        self.client_id = None

    def queue(self, payload):
        self.payloads.append(payload)

    async def close(self, reason):
        self.closed.append(reason)


class Factory:
    def __init__(self, clock=None):
        self.clock = clock or Clock()
        self.sessions, self.bridges, self.workers = [], [], []

    async def __call__(self, session_id, guard):
        bridge, worker = FakeBridge(), CaptionWorker()
        registry = SourceRegistry([MatchSource("cam-1", "Synthetic recording",
            source_url="https://recordings.invalid/synthetic.mp4")])
        prepared = SessionConfig(monitor=True, mode="real", model_version=NEW_MODEL_VERSION,
            checkpoint=NEW_MODEL_VERSION, max_duration_seconds=120,
            max_estimated_cost_usd=.70, max_restarts=0, gpu_workers=1)
        session = await ReplaySession(registry, session_id=session_id,
            prepared_config=prepared, bridge_factory=lambda: bridge,
            caption_worker=worker, activation_guard=guard,
            now=self.clock, monotonic=self.clock).initialize()
        session._caption_user_enabled = session._caption_enabled = False
        self.sessions.append(session)
        self.bridges.append(bridge)
        self.workers.append(worker)
        return session


class Routes:
    def __init__(self):
        self.closed = False

    async def dispatch(self, path, query):
        return {"synthetic": True} if path == "/api/catalog" else None

    async def close(self):
        self.closed = True


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)
