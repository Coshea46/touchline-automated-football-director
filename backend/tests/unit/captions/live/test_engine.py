"""Offline tests for unit captions live test_engine."""


import asyncio
import base64
import json
import sys
import time
from threading import Event
import pytest
from touchline.captions.live.engine import RealtimeEngine, valid_control
from touchline.captions.live.adapter import RealtimeCaptionWorker
from tests.support.captions.realtime import control, until, Audio, Connection


def test_partial_then_committed_source_timestamps_no_future_pcm_and_generation_cancel():
    async def scenario():
        Audio.instances = []
        latest, events, connections = [control()], [], []
        async def connect(_):
            return Connection(connections)
        engine = RealtimeEngine(lambda: latest[0], events.append, api_key_present=True,
                                audio_factory=Audio, connect_factory=connect, identity_checker=lambda *a: "a" * 64)
        task = asyncio.create_task(engine.run())
        await until(lambda: connections)
        await asyncio.sleep(.05)
        assert not connections[0].sends  # position0 permits no future audio
        latest[0] = control(position=.5)
        await until(lambda: sum(len(base64.b64decode(x["audio_base_64"])) for x in connections[0].sends) == 16000)
        partial = next(event for event in events if event["state"] == "partial")
        committed = next(event for event in events if event["state"] == "committed")
        assert "words" not in partial
        assert committed["words"][0]["start_seconds"] == 120
        assert committed["words"][0]["end_seconds"] == 120.1
        assert max(reader.position for reader in Audio.instances) <= 120.5
        latest[0] = control(generation=2, clock_epoch=1, match_id="cam-2", source_key="match-2.mp4", position=1)
        await until(lambda: len(connections) == 2)
        before = len(events)
        connections[0].handlers["partial_transcript"]({"text": "STALE SECRET"})
        assert len(events) == before
        assert connections[0].closed
        latest[0] = control(generation=2, active=False)
        await until(lambda: connections[1].closed)
        engine.closed = True
        await task
        assert all(conn.closed for conn in connections)
        assert all("SECRET" not in json.dumps(event) for event in events)
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [{"playing": False}, {"stalled": True}, {"available": False},
                                      {"expires_at": 0}, {"deadline": 0}, {"deadline": float("inf")}])
def test_pause_stall_lease_and_deadline_are_authoritative(changes):
    assert not valid_control(control(**changes))


def test_missing_key_and_identity_mismatch_never_connect():
    async def scenario():
        for key, identity, reason in [(False, "a" * 64, "api_key_missing"),
                                      (True, "b" * 64, "source_identity_changed")]:
            events = []
            async def forbidden(_):
                pytest.fail("Missing keys or mismatched sources must not connect")
            engine = RealtimeEngine(lambda: control(), events.append, api_key_present=key,
                                    audio_factory=Audio, connect_factory=forbidden, identity_checker=lambda *a, identity=identity: identity)
            await engine._stream(control())
            engine._executor.shutdown(wait=True)
            assert events[-1]["reason"] == reason and events[-1]["state"] == "unavailable"
    asyncio.run(scenario())


def test_provider_error_is_sanitized_and_same_generation_never_retries():
    async def scenario():
        latest, events, attempts = [control()], [], []
        async def fail(_):
            attempts.append(True)
            raise RuntimeError("PRIVATE_KEY_AND_URL")
        engine = RealtimeEngine(lambda: latest[0], events.append, api_key_present=True,
                                audio_factory=Audio, connect_factory=fail, identity_checker=lambda *a: "a" * 64)
        task = asyncio.create_task(engine.run())
        await until(lambda: any(event["state"] == "error" for event in events))
        assert next(event for event in events if event["state"] == "error")["reason"] == "caption_runtimeerror"
        for _ in range(5):
            latest[0] = control(position=.5)
            await asyncio.sleep(.04)
        assert len(attempts) == 1
        assert "PRIVATE" not in json.dumps(events)
        engine.closed = True
        await task
    asyncio.run(scenario())


def test_delayed_decode_cannot_send_after_revocation():
    async def scenario():
        entered, release = Event(), Event()
        class SlowAudio(Audio):
            def pull(self, *args):
                entered.set()
                release.wait(1)
                return super().pull(*args)
        latest, events, connections = [control()], [], []
        async def connect(_):
            return Connection(connections)
        engine = RealtimeEngine(lambda: latest[0], events.append, api_key_present=True,
                                audio_factory=SlowAudio, connect_factory=connect, identity_checker=lambda *a: "a" * 64)
        task = asyncio.create_task(engine.run())
        await until(entered.is_set)
        latest[0] = control(active=False)
        await until(lambda: connections[0].closed)
        release.set()
        engine.closed = True
        await task
        assert connections[0].sends == []
    asyncio.run(scenario())


def test_pcm_budget_survives_generation_changes():
    async def scenario():
        latest, events, connections = [control()], [], []
        async def connect(_):
            return Connection(connections)
        engine = RealtimeEngine(lambda: latest[0], events.append, api_key_present=True,
                                max_audio_seconds=2, audio_factory=Audio, connect_factory=connect,
                                identity_checker=lambda *a: "a" * 64)
        task = asyncio.create_task(engine.run())
        await until(lambda: connections)
        latest[0] = control(position=1.5)
        await until(lambda: engine.metrics["pcm_seconds"] >= 1.5)
        latest[0] = control(generation=2, position=1.5)
        await until(lambda: len(connections) == 2)
        latest[0] = control(generation=2, position=3)
        await until(lambda: any(event.get("reason") == "budget_exhausted" for event in events))
        assert engine.metrics["pcm_seconds"] <= 2
        latest[0] = control(generation=3, position=3)
        await asyncio.sleep(.1)
        assert len(connections) == 2
        engine.closed = True
        await task
    asyncio.run(scenario())


def test_adapter_reports_unexpected_child_eof_and_closes_without_sdk():
    from io import StringIO
    class Process:
        def __init__(self, *args, **kwargs):
            self.stdin, self.stdout = StringIO(), StringIO("")

        def wait(self, timeout):
            return 0
    events = []
    worker = RealtimeCaptionWorker(events.append, python_executable=sys.executable, process_factory=Process)
    worker.update(control())
    end = time.monotonic() + 1
    while not events and time.monotonic() < end:
        time.sleep(.01)
    assert events[-1]["reason"] == "caption_worker_ended"
    assert "SECRET" not in json.dumps(events)
    assert worker.close() is True


def test_unconfirmed_provider_close_prohibits_every_later_generation():
    async def scenario():
        latest, events, connections = [control()], [], []
        class FailedClose(Connection):
            async def close(self):
                raise RuntimeError("PRIVATE_PROVIDER_CLOSE")
        async def connect(_):
            return FailedClose(connections)
        engine = RealtimeEngine(lambda: latest[0], events.append, api_key_present=True,
                                audio_factory=Audio, connect_factory=connect, identity_checker=lambda *a: "a" * 64)
        task = asyncio.create_task(engine.run())
        await until(lambda: connections)
        latest[0] = control(generation=2)
        await until(lambda: any(event.get("reason") == "cleanup_unconfirmed" and event["generation"] == 2 for event in events))
        assert len(connections) == 1 and engine.metrics["forced_provider_closes"] == 1
        assert "PRIVATE" not in json.dumps(events)
        engine.closed = True
        await task
    asyncio.run(scenario())


def test_custom_lower_cost_limit_reserves_provider_close_time():
    async def scenario():
        clock, latest, connections = [0.0], [control()], []

        class SlowClose(Connection):
            async def close(self):
                clock[0] += .9
                self.closed = True

        async def connect(_):
            return SlowClose(connections)

        engine = RealtimeEngine(lambda: latest[0], lambda event: None,
                                api_key_present=True, max_cost_usd=.001,
                                audio_factory=Audio, connect_factory=connect,
                                identity_checker=lambda *a: "a" * 64, now=lambda: clock[0])
        task = asyncio.create_task(engine.run())
        try:
            await until(lambda: connections)
            # $0.001 permits about 9.23 connected seconds. Close needs .9s;
            # waiting for the dollar cap itself would exceed the allowance.
            clock[0] = 8.2
            await until(lambda: connections[0].closed)
            assert engine._totals()["estimated_cost_usd"] <= .001
            latest[0] = control(generation=2)
            await asyncio.sleep(.06)
            assert len(connections) == 1
        finally:
            engine.closed = True
            await task

    asyncio.run(scenario())
