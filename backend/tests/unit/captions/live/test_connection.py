"""Offline tests for unit captions live test_connection."""


import asyncio
import sys
from types import ModuleType, SimpleNamespace
import pytest
from touchline.captions.live.connection import BufferedRealtimeConnection, official_connect
from touchline.captions.live.engine import RealtimeEngine
from tests.support.captions.realtime import Audio, Connection, control, until
from tests.support.captions.provider import RawConnection


@pytest.mark.parametrize("code", ["auth_error", "unaccepted_terms", "quota_exceeded", "invalid_request"])
def test_sdk_error_before_engine_listener_registration_is_retained_and_sanitized(code):
    async def check():
        raw = RawConnection([])
        buffered = BufferedRealtimeConnection(raw)
        raw.handlers["error"]({"message_type": code, "error": "PRIVATE_API_KEY_OR_SIGNED_URL"})
        raw.handlers["close"]({"code": 1000, "reason": "PRIVATE_CLOSE_REASON"})
        errors, closes = [], []
        buffered.on("error", errors.append)
        buffered.on("close", closes.append)
        assert errors == [{"message_type": code}]
        assert closes == [{"code": 1000}]
        assert not buffered._pending
    asyncio.run(check())


def test_direct_official_connect_installs_handlers_before_scheduled_sdk_receiver(monkeypatch):
    async def check():
        connections, pools = [], []

        class HTTP:
            def __init__(self, **_):
                self.closed = False
                pools.append(self)

            def close(self):
                self.closed = True

        async def connect(_):
            raw = RawConnection(connections)

            async def immediate_receiver():
                # The real SDK schedules this task immediately before returning.
                # Nested wait_for in official_connect lets it run too early.
                assert "error" in raw.handlers
                raw.handlers["error"]({"message_type": "unaccepted_terms", "error": "DO_NOT_EXPOSE"})
                raw.handlers["close"]({"code": 1000, "reason": "DO_NOT_EXPOSE"})

            asyncio.create_task(immediate_receiver())
            return raw

        class Client:
            def __init__(self, **_):
                self.speech_to_text = SimpleNamespace(realtime=SimpleNamespace(connect=connect))

        elevenlabs, client_module, httpx = ModuleType("elevenlabs"), ModuleType("elevenlabs.client"), ModuleType("httpx")
        elevenlabs.AudioFormat = SimpleNamespace(PCM_16000="pcm_16000")
        elevenlabs.CommitStrategy = SimpleNamespace(VAD="vad")
        client_module.ElevenLabs, httpx.Client = Client, HTTP
        monkeypatch.setitem(sys.modules, "elevenlabs", elevenlabs)
        monkeypatch.setitem(sys.modules, "elevenlabs.client", client_module)
        monkeypatch.setitem(sys.modules, "httpx", httpx)
        wrapped = await asyncio.wait_for(official_connect(control()), 1)
        await asyncio.sleep(0)  # SDK receiver runs before Engine resumes/listens.
        errors = []
        wrapped.on("error", errors.append)
        assert errors == [{"message_type": "unaccepted_terms"}]
        await wrapped.close()
        wrapped._caption_http_pool.close()
        assert pools[0].closed and connections[0].closed
    asyncio.run(check())


def test_provider_error_is_not_masked_by_following_send_failure():
    async def check():
        latest, events, connections = [control()], [], []

        class FailingSend(Connection):
            async def send(self, value):
                self.handlers["error"]({"message_type": "input_error", "error": "DO_NOT_EXPOSE"})
                raise RuntimeError("ConnectionClosedOK with private details")

        async def connect(_):
            return FailingSend(connections)

        engine = RealtimeEngine(lambda: latest[0], events.append, api_key_present=True,
                                audio_factory=Audio, connect_factory=connect, identity_checker=lambda *args: "a" * 64)
        task = asyncio.create_task(engine.run())
        await until(lambda: connections)
        latest[0] = control(position=.5)
        await until(lambda: any(event.get("reason") == "input_error" for event in events))
        assert "ConnectionClosedOK" not in str(events) and "DO_NOT_EXPOSE" not in str(events)
        engine.closed = True
        await task
    asyncio.run(check())


def test_engine_observes_early_terms_error_before_submitting_any_pcm():
    async def check():
        latest, events, connections = [control()], [], []

        async def connect(_):
            raw = RawConnection(connections)
            wrapped = BufferedRealtimeConnection(raw)
            raw.handlers["error"]({"message_type": "unaccepted_terms", "error": "DO_NOT_EXPOSE"})
            raw.handlers["close"]({"code": 1000, "reason": "DO_NOT_EXPOSE"})
            return wrapped

        engine = RealtimeEngine(lambda: latest[0], events.append, api_key_present=True,
                                audio_factory=Audio, connect_factory=connect, identity_checker=lambda *args: "a" * 64)
        task = asyncio.create_task(engine.run())
        await until(lambda: any(event.get("reason") == "unaccepted_terms" for event in events))
        assert connections[0].sends == [] and connections[0].closed
        assert engine.metrics["provider_close_code"] == 1000
        assert not engine.metrics["provider_session_started"]
        assert not any(event["state"] == "listening" for event in events)
        assert "DO_NOT_EXPOSE" not in str(events)
        engine.closed = True
        await task
    asyncio.run(check())
