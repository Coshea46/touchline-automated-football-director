"""Reusable synthetic test doubles and builders; no provider calls."""


import asyncio
import time
from typing import ClassVar
from touchline.captions.live.audio import AudioChunk


def control(**changes):
    return {"session_id": "fixture-session", "match_id": "cam-1", "source_key": "match-1.mp4",
            "source_identity": "a" * 64, "source_url": "https://test.invalid/private?sig=SECRET",
            "generation": 1, "clock_epoch": 0, "offset_seconds": 120, "position": 0,
            "active": True, "playing": True, "stalled": False, "available": True,
            "expires_at": time.time() + 5, "deadline": time.time() + 30,
            "report_received_at": time.time(), **changes}


async def until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "bounded fake operation did not finish"
        await asyncio.sleep(.01)


class Audio:
    instances: ClassVar[list] = []

    def __init__(self, url, start):
        self.position, self.closed, self.permits = start, False, []
        self.instances.append(self)

    def pull(self, permit, maximum):
        self.permits.append(permit)
        count = max(0, int(min(maximum, permit - self.position) * 16000 + 1e-7))
        if not count:
            return None
        start = self.position
        self.position += count / 16000
        return AudioChunk(b"\x00\x00" * count, start, self.position)

    def close(self):
        self.closed = True


class Connection:
    def __init__(self, connections):
        assert not any(not conn.closed for conn in connections), "more than one active provider connection"
        self.closed, self.handlers, self.sends = False, {}, []
        connections.append(self)

    def on(self, name, handler):
        self.handlers[name] = handler

    async def send(self, value):
        self.sends.append(value)
        self.handlers["partial_transcript"]({"text": "TEST partial"})
        self.handlers["committed_transcript_with_timestamps"]({"text": "TEST committed", "words": [
            {"text": "TEST", "start": 0, "end": .1, "type": "word"}]})

    async def close(self):
        self.closed = True
