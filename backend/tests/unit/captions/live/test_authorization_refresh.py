"""Signed URL rotation preserves a focused stream's identity and allowance."""

import asyncio

import pytest

from touchline.captions.live.engine import RealtimeEngine, valid_control
from tests.support.captions.realtime import Audio, Connection, control, until


@pytest.mark.parametrize("head_url", [
    "https://test.invalid/other-recording.mp4?signature=PRIVATE",
    "http://test.invalid/match-1.mp4?signature=PRIVATE",
    "https://user:PRIVATE@test.invalid/match-1.mp4",
])
def test_live_control_rejects_a_head_url_that_cannot_verify_its_media(head_url):
    assert not valid_control(control(source_url="https://test.invalid/match-1.mp4?GET=PRIVATE",
                                     source_identity_url=head_url))


def test_url_rotation_keeps_one_decoder_provider_and_original_caption_allowance():
    async def scenario():
        connections, requests, opened, events = [], [], [], []

        def source(version, position=0, **extra):
            return control(source_url=f"https://test.invalid/match-1.mp4?GET_PRIVATE={version}",
                           source_identity_url=f"https://test.invalid/match-1.mp4?HEAD_PRIVATE={version}",
                           position=position, **extra)

        latest = [source(1)]

        class RecordingAudio(Audio):
            def __init__(self, url, start):
                opened.append((url, start))
                super().__init__(url, start)

        async def connect(_):
            return Connection(connections)

        def checker(*args):
            requests.append(args)
            return "a" * 64

        engine = RealtimeEngine(lambda: latest[0], events.append, api_key_present=True,
            max_audio_seconds=2, audio_factory=RecordingAudio,
            connect_factory=connect, identity_checker=checker)
        task = asyncio.create_task(engine.run())
        try:
            await until(lambda: connections)
            deadline = engine._deadline
            latest[0] = source(2, position=1.5, deadline=deadline + 1000)
            await until(lambda: engine.metrics["pcm_seconds"] >= 1.5)
            assert len(connections) == len(opened) == 1
            assert engine._deadline == deadline
            latest[0] = source(3, position=3, deadline=deadline + 1000)
            await until(lambda: any(event.get("reason") == "budget_exhausted" for event in events))
            assert len(connections) == len(opened) == len(requests) == 1
            assert requests[0] == ("match-1.mp4", source(1)["source_url"], source(1)["source_identity_url"])
            assert engine.metrics["pcm_seconds"] <= 2
            assert connections[0].closed
        finally:
            engine.closed = True
            await task

    asyncio.run(scenario())


def test_later_generation_opens_the_latest_get_url_without_renewing_its_allowance():
    async def scenario():
        opened, connections, checks = [], [], []
        get_url = "https://test.invalid/match-1.mp4?GET_PRIVATE=1"
        head_url = "https://test.invalid/match-1.mp4?HEAD_PRIVATE=1"
        latest = [control(source_url=get_url, source_identity_url=head_url)]

        class RecordingAudio(Audio):
            def __init__(self, url, start):
                opened.append((url, start))
                super().__init__(url, start)

        async def connect(_):
            return Connection(connections)

        def checker(*args):
            checks.append(args)
            return "a" * 64

        engine = RealtimeEngine(lambda: latest[0], lambda event: None, api_key_present=True,
            audio_factory=RecordingAudio, connect_factory=connect, identity_checker=checker)
        task = asyncio.create_task(engine.run())
        try:
            await until(lambda: connections)
            deadline = engine._deadline
            latest[0] = control(source_url=get_url, source_identity_url=head_url, position=.5)
            await until(lambda: engine.metrics["pcm_seconds"] >= .5)
            renewed_get = "https://test.invalid/match-1.mp4?GET_PRIVATE=2"
            latest[0] = control(source_url=renewed_get,
                source_identity_url="https://test.invalid/match-1.mp4?HEAD_PRIVATE=2",
                generation=2, position=.5, deadline=deadline + 1000)
            await until(lambda: len(connections) == 2)
            assert opened == [(get_url, 120), (renewed_get, 120.5)]
            assert len(checks) == 1  # Still inside the same object identity's bounded cache.
            assert engine.metrics["pcm_seconds"] == .5
            assert engine._deadline == deadline
        finally:
            engine.closed = True
            await task

    asyncio.run(scenario())
