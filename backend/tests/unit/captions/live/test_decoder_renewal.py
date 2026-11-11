"""A renewed media signature replaces only the native reader, never Scribe."""

import asyncio
import base64
from threading import Event
import time

import pytest

from touchline.captions.errors import CaptionError
from touchline.captions.live.engine import RealtimeEngine, valid_control
from tests.support.captions.realtime import Audio, Connection, control, until


@pytest.mark.parametrize("expiry", [float("nan"), float("inf"), True, -1, "tomorrow"])
def test_malformed_source_expiry_cannot_authorize_audio(expiry):
    assert not valid_control(control(source_url_expires_at=expiry))


def test_near_expiry_reader_is_renewed_at_sent_pcm_with_one_provider_and_budget():
    async def scenario():
        base = time.time()
        readers, connections, chunks, events = [], [], [], []

        def view(version, position=0):
            return control(source_url=f"https://test.invalid/match-1.mp4?GET_PRIVATE={version}",
                source_identity_url=f"https://test.invalid/match-1.mp4?HEAD_PRIVATE={version}",
                source_url_expires_at=base + (5 if version == 1 else 60), position=position)

        latest = [view(1)]

        class RecordingAudio(Audio):
            def __init__(self, url, start):
                assert all(reader.closed for reader in readers)
                self.url, self.start = url, start
                readers.append(self)
                super().__init__(url, start)

            def pull(self, *args):
                chunk = super().pull(*args)
                if chunk:
                    chunks.append((chunk.start_seconds, chunk.end_seconds))
                return chunk

        async def connect(_):
            return Connection(connections)

        engine = RealtimeEngine(lambda: latest[0], events.append, api_key_present=True,
            max_audio_seconds=2, audio_factory=RecordingAudio,
            connect_factory=connect, identity_checker=lambda *args: "a" * 64)
        task = asyncio.create_task(engine.run())
        try:
            await until(lambda: connections)
            deadline = engine._deadline
            latest[0] = view(1, .5)
            await until(lambda: engine.metrics["pcm_seconds"] >= .5)
            latest[0] = view(2, 3)
            await until(lambda: any(event.get("reason") == "budget_exhausted" for event in events))
            assert len(readers) == 2 and readers[0].closed
            assert readers[1].start == 120.5
            assert readers[1].url == view(2)["source_url"]
            assert len(connections) == engine.metrics["provider_sessions"] == 1
            assert engine.metrics["audio_source_reopens"] == 1
            assert engine.metrics["pcm_seconds"] == 2
            assert sum(len(base64.b64decode(send["audio_base_64"])) for send in connections[0].sends) == 64000
            assert chunks[0][0] == 120 and chunks[-1][1] == 122
            assert all(start == chunks[index - 1][1] for index, (start, _) in enumerate(chunks) if index)
            assert engine._deadline == deadline
            assert {event["source_anchor_seconds"] for event in events if event.get("text")} == {120}
        finally:
            engine.closed = True
            await task

    asyncio.run(scenario())


def test_cancelled_reopen_closes_its_late_reader_without_sending_more_pcm():
    async def scenario():
        base = time.time()
        entered, release = Event(), Event()
        readers, connections = [], []

        def view(version, position=0, **extra):
            return control(source_url=f"https://test.invalid/match-1.mp4?GET_PRIVATE={version}",
                source_url_expires_at=base + (5 if version == 1 else 60), position=position, **extra)

        latest = [view(1)]

        def factory(url, start):
            if "GET_PRIVATE=2" in url:
                assert readers[0].closed
                entered.set()
                release.wait(1)
            reader = Audio(url, start)
            readers.append(reader)
            return reader

        async def connect(_):
            return Connection(connections)

        engine = RealtimeEngine(lambda: latest[0], lambda event: None, api_key_present=True,
            audio_factory=factory, connect_factory=connect, identity_checker=lambda *args: "a" * 64)
        task = asyncio.create_task(engine.run())
        try:
            await until(lambda: connections)
            latest[0] = view(1, .5)
            await until(lambda: engine.metrics["pcm_seconds"] >= .5)
            sent = len(connections[0].sends)
            latest[0] = view(2, 1)
            await until(entered.is_set)
            latest[0] = view(2, 1, active=False)
            await until(lambda: connections[0].closed)
            engine.closed = True
            await task
            release.set()
            await until(lambda: len(readers) == 2 and readers[1].closed)
            assert len(connections) == 1 and readers[0].closed
            assert len(connections[0].sends) == sent
            assert engine.metrics["pcm_seconds"] == .5
        finally:
            release.set()
            engine.closed = True
            await task

    asyncio.run(scenario())


def test_failed_reopen_is_sanitized_and_never_retries_same_generation():
    async def scenario():
        base = time.time()
        attempts, connections, events = [], [], []

        def view(version):
            return control(source_url=f"https://test.invalid/match-1.mp4?GET_PRIVATE={version}",
                           source_url_expires_at=base + (5 if version == 1 else 60))

        latest = [view(1)]

        def factory(url, start):
            attempts.append(url)
            if len(attempts) > 1:
                raise CaptionError("audio_open_oserror")
            return Audio(url, start)

        async def connect(_):
            return Connection(connections)

        engine = RealtimeEngine(lambda: latest[0], events.append, api_key_present=True,
            audio_factory=factory, connect_factory=connect, identity_checker=lambda *args: "a" * 64)
        task = asyncio.create_task(engine.run())
        try:
            await until(lambda: connections)
            latest[0] = view(2)
            await until(lambda: any(event.get("reason") == "audio_open_oserror" for event in events))
            latest[0] = view(3)
            await asyncio.sleep(.1)
            assert len(attempts) == 2 and len(connections) == 1
            assert connections[0].closed
        finally:
            engine.closed = True
            await task

    asyncio.run(scenario())
