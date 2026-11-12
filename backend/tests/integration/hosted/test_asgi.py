"""Exercise real ASGI handlers and replay sessions using synthetic queues only."""

import asyncio
import json

import pytest

from touchline.hosted.app import create_app
from tests.support.hosted import Admission, Factory, Routes, until

pytestmark = pytest.mark.integration
ORIGIN = "https://portfolio.invalid"


class Client:
    def __init__(self, app, *, origin=ORIGIN, peer="192.0.2.10", headers=()):
        self.incoming, self.outgoing = asyncio.Queue(), []
        self.app = app
        self.scope = {"type": "websocket", "path": "/ws", "query_string": b"",
            "client": (peer, 123), "headers": [(b"origin", origin.encode()), *headers]}
        self.task = None

    async def start(self, hello):
        await self.incoming.put({"type": "websocket.connect"})
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps(hello)})
        async def send(event):
            self.outgoing.append(event)
        self.task = asyncio.create_task(self.app(self.scope, self.incoming.get, send))
        return self

    def messages(self, kind):
        return [value for event in self.outgoing if event["type"] == "websocket.send"
                and (value := json.loads(event["text"])).get("type") == kind]

    async def message(self, value):
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps(value)})

    async def close(self):
        if self.task and not self.task.done():
            await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
        if self.task:
            await self.task


def test_native_asgi_two_tabs_get_private_auth_before_independent_snapshots_and_sticky_resume():
    async def scenario():
        admission, factory, routes = Admission(), Factory(), Routes()
        app = create_app(env={"MATCHPULSE_ALLOWED_ORIGINS": ORIGIN}, admission=admission,
                         session_factory=factory, routes=routes, now=factory.clock)
        a, b, resumed = Client(app), Client(app, peer="192.0.2.20"), Client(app)
        try:
            await a.start({"type": "hello"})
            await until(lambda: a.messages("session"))
            auth = a.messages("session_auth")[0]
            assert [json.loads(event["text"])["type"] for event in a.outgoing
                    if event["type"] == "websocket.send"][:2] == ["session_auth", "session"]
            assert a.messages("session")[0]["playback"]["position"] == 0
            await a.message({"type": "playback", "session_id": auth["session_id"],
                "match_id": "cam-1", "position": 73, "playing": True, "rate": 1, "stalled": False})
            await until(lambda: factory.sessions[0].position == 73)
            await b.start({"type": "hello"})
            await until(lambda: b.messages("session"))
            assert b.messages("session")[0]["playback"]["position"] == 0
            assert auth["resume_token"] not in json.dumps(b.outgoing)
            await a.close()
            await resumed.start({"type": "hello", "session_id": auth["session_id"],
                "resume_token": auth["resume_token"]})
            await until(lambda: resumed.messages("session"))
            assert resumed.messages("session_auth")[0]["resumed"]
            assert resumed.messages("session")[0]["playback"]["position"] == 73
            assert len(factory.sessions) == 2
        finally:
            await a.close()
            await b.close()
            await resumed.close()
            await app.close()
        assert routes.closed and admission.closed and all(session._closed for session in factory.sessions)
    asyncio.run(scenario())


def test_origin_rejection_and_gateway_peer_ignore_browser_forwarding_headers():
    async def scenario():
        admission, factory = Admission(), Factory()
        app = create_app(env={"MATCHPULSE_ALLOWED_ORIGINS": ORIGIN}, admission=admission,
            session_factory=factory, routes=Routes(), now=factory.clock)
        bad = Client(app, origin="https://attacker.invalid")
        valid = Client(app, headers=[(b"x-forwarded-for", b"forged"), (b"forwarded", b"for=forged")])
        try:
            await bad.start({"type": "hello"})
            await until(lambda: bad.task.done())
            assert bad.outgoing == [{"type": "websocket.close", "code": 4403}]
            assert not admission.connections
            await valid.start({"type": "hello"})
            await until(lambda: valid.messages("session_auth"))
            assert admission.connections == admission.creations == ["192.0.2.10"]
        finally:
            await valid.close()
            await app.close()
    asyncio.run(scenario())


def test_rejected_resume_is_closed_with_explicit_code_and_never_fresh_auth():
    async def scenario():
        admission, factory = Admission(), Factory()
        app = create_app(env={"MATCHPULSE_ALLOWED_ORIGINS": ORIGIN}, admission=admission,
            session_factory=factory, routes=Routes())
        client = Client(app)
        try:
            await client.start({"type": "hello", "session_id": "a" * 32, "resume_token": "b" * 43})
            await client.task
            assert client.messages("error")[0]["code"] == "session_expired"
            assert client.outgoing[-1]["code"] == 4404
            assert not factory.sessions and not client.messages("session_auth")
        finally:
            await app.close()
    asyncio.run(scenario())


def test_http_metadata_cors_health_and_rate_retry_are_native_asgi_and_sanitized():
    async def scenario():
        from touchline.hosted.admission import AdmissionDenied
        admission, routes = Admission(), Routes()
        app = create_app(env={"MATCHPULSE_ALLOWED_ORIGINS": ORIGIN}, admission=admission,
            session_factory=Factory(), routes=routes)
        async def request(path, origin=ORIGIN):
            output = []
            async def send(event):
                output.append(event)
            await app({"type": "http", "method": "GET", "path": path,
                "query_string": b"", "headers": [(b"origin", origin.encode())],
                "client": ("192.0.2.10", 123)}, None, send)
            return output
        try:
            response = await request("/api/catalog")
            assert response[0]["status"] == 200 and json.loads(response[1]["body"]) == {"synthetic": True}
            assert (b"access-control-allow-origin", ORIGIN.encode()) in response[0]["headers"]
            assert (await request("/healthz"))[0]["status"] == 200
            assert (await request("/api/catalog", "https://attacker.invalid"))[0]["status"] == 403
            async def limited(peer):
                raise AdmissionDenied("rate_limited", 19)
            admission.allow_http = limited
            response = await request("/api/catalog")
            assert response[0]["status"] == 429
            assert (b"retry-after", b"19") in response[0]["headers"]
        finally:
            await app.close()
    asyncio.run(scenario())


def test_expiry_retires_idle_asgi_handler_even_when_peer_never_acknowledges_close():
    async def scenario():
        admission, factory = Admission(), Factory()
        app = create_app(env={"MATCHPULSE_ALLOWED_ORIGINS": ORIGIN}, admission=admission,
            session_factory=factory, routes=Routes(), now=factory.clock)
        client = Client(app)
        try:
            await client.start({"type": "hello"})
            await until(lambda: client.messages("session"))
            factory.clock.value += 1200
            await app.sessions.sweep()
            # Client.incoming stays blocked: no synthetic disconnect is sent.
            await asyncio.wait_for(client.task, 1)
            assert client.outgoing[-1] == {"type": "websocket.close", "code": 4404,
                                           "reason": "session_expired"}
            assert not app._handlers and not app.sessions.visitors
        finally:
            await app.close()
    asyncio.run(scenario())
