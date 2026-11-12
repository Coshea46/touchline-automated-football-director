"""Slow financial commits cannot hold the ASGI reader or outlive ownership."""

import asyncio
import json

import pytest

from touchline.hosted.admission import AdmissionDenied
from touchline.hosted.app import create_app
from touchline.hosted.sessions import SessionManager
from tests.integration.hosted.test_asgi import Client, ORIGIN
from tests.support.hosted import Admission, Connection, Factory, Routes, until
from tests.support.replay.session import playback

pytestmark = pytest.mark.integration


def control(visitor, kind, enabled):
    return {"type": kind, "session_id": visitor.session.session_id, "enabled": enabled}


@pytest.mark.parametrize("component,kind", [("director", "auto"), ("captions", "captions")])
@pytest.mark.parametrize("outcome", ["grant", "denial"])
def test_reader_processes_playback_duplicates_and_off_during_slow_admission(component, kind, outcome):
    async def scenario():
        factory, admission = Factory(), Admission()
        app = create_app(env={"MATCHPULSE_ALLOWED_ORIGINS": ORIGIN}, admission=admission,
            session_factory=factory, routes=Routes(), now=factory.clock)
        client = Client(app)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        original = admission.acquire_ai
        async def acquire(session_id, peer, *, component="director"):
            calls.append(component)
            if component == target:
                entered.set()
                await release.wait()
                if outcome == "denial":
                    raise AdmissionDenied("ai_daily_limit", 12)
            return await original(session_id, peer, component=component)
        target = component
        try:
            await client.start({"type": "hello"})
            await until(lambda: client.messages("session_auth"))
            visitor = next(iter(app.sessions.visitors.values()))
            # Give the other feature a real, independent fake-provider lease.
            if component == "director":
                await client.message(control(visitor, "captions", True))
                await until(lambda: visitor.captions_active)
                other_permit = visitor.captions_permit
            else:
                await client.message(playback(visitor.session, available=True))
                await until(lambda: visitor.ai_active and factory.bridges[0].calls)
                other_permit = visitor.ai_permit
            admission.acquire_ai = acquire
            worker_starts = sum(call[0] == "start" for call in factory.workers[0].calls)
            before_start_sequence = visitor.session.sequence
            if component == "director":
                # The default Director preference follows the same deferred
                # path as an explicit On, after a factual native report only.
                await client.message(playback(visitor.session, available=True))
            else:
                await client.message(control(visitor, kind, True))
            await until(entered.is_set)
            if component == "director":
                await until(lambda: any(row["auto_starting"] and not row["ai_active"]
                                       for row in client.messages("session")))
                # The frontend rejects equal-sequence snapshots. Pending
                # admission must have its own factual protocol transition.
                starting = [row for row in client.messages("session")
                            if row["auto_starting"] and not row["ai_active"]][-1]
                assert starting["sequence"] > before_start_sequence
                assert visitor.session.bridge is None
            await client.message(control(visitor, kind, True))
            for position in range(1, 9):
                await client.message(playback(visitor.session, position=position, available=True))
            await client.message(control(visitor, kind, False))
            await until(lambda: visitor.session.position == 8 and
                visitor.feature_epochs[component] != visitor.pending_starts[component].epoch)
            assert not release.is_set() and calls == [component]
            assert not client.task.done() and not client.messages("error")
            if component == "director":
                assert not visitor.session.director.auto_enabled
                assert visitor.captions_active and visitor.session._caption_enabled
                assert not app.sessions.snapshot(visitor, visitor.session.snapshot())["auto_starting"]
                assert visitor.session.sequence > starting["sequence"]
                assert not factory.bridges[0].calls
            else:
                assert visitor.ai_active and visitor.session.director.auto_enabled
                assert not visitor.session._caption_enabled
                assert sum(call[0] == "start" for call in factory.workers[0].calls) == worker_starts
            # A second On is refused while the canceled financial operation
            # drains. It cannot bypass the component's pending reservation.
            await client.message(control(visitor, kind, True))
            await until(lambda: client.messages("error"))
            assert client.messages("error")[-1]["code"] == "ai_busy"
            assert client.messages("error")[-1]["feature"] == component
            before = (visitor.ai_reason, visitor.session.director.auto_enabled,
                      visitor.session._caption_enabled)
            release.set()
            await until(lambda: not visitor.pending_starts)
            assert (visitor.ai_reason, visitor.session.director.auto_enabled,
                    visitor.session._caption_enabled) == before
            assert len(client.messages("error")) == 1  # No late denial/error.
            assert admission.released == ([("permit-1", True)] if outcome == "grant" else [])
            assert other_permit not in [permit for permit, _ in admission.released]
            if component == "director":
                assert visitor.captions_active and visitor.captions_permit == other_permit
                assert not factory.bridges[0].calls
            else:
                assert visitor.ai_active and visitor.ai_permit == other_permit
                assert sum(call[0] == "start" for call in factory.workers[0].calls) == worker_starts
        finally:
            release.set()
            await client.close()
            await app.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind,component", [("auto", "director"), ("captions", "captions")])
def test_deferred_current_denial_is_feature_tagged_and_delivered_to_its_owner(kind, component):
    async def scenario():
        factory, admission, connection = Factory(), Admission(), Connection()
        manager = SessionManager(factory, admission, now=factory.clock)
        try:
            visitor, _ = await manager.attach({"type": "hello"}, "peer", connection)
            admission.denial = "ai_rate_limited"
            assert await manager.receive(visitor, connection, control(visitor, kind, True),
                                         defer_paid_start=True) is None
            await until(lambda: any(row["type"] == "error" for row in connection.payloads))
            error = [row for row in connection.payloads if row["type"] == "error"][-1]
            assert (error["feature"], error["code"], error["retry_after"]) == (component, "rate_limited", 12)
            assert not admission.acquired and not factory.bridges[0].calls
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_shutdown_drains_commit_and_unused_financial_close_before_source_client_cleanup():
    async def scenario():
        factory, admission = Factory(), Admission()
        entered, grant, releasing, released = (asyncio.Event() for _ in range(4))
        closed = []
        original_acquire, original_release = admission.acquire_ai, admission.release_ai
        async def acquire(session_id, peer, *, component="director"):
            entered.set()
            await grant.wait()
            return await original_acquire(session_id, peer, component=component)
        async def release(permit, confirmed=False):
            releasing.set()
            await released.wait()
            await original_release(permit, confirmed)
        admission.acquire_ai, admission.release_ai = acquire, release
        class Source:
            def close(self):
                closed.append("source")
                return True
        app = create_app(env={"MATCHPULSE_ALLOWED_ORIGINS": ORIGIN}, admission=admission,
            session_factory=factory, routes=Routes(), now=factory.clock, source_access=Source())
        client, closing = Client(app), None
        try:
            await client.start({"type": "hello"})
            await until(lambda: client.messages("session_auth"))
            visitor = next(iter(app.sessions.visitors.values()))
            await client.message(playback(visitor.session))
            await until(entered.is_set)
            closing = asyncio.create_task(app.close())
            await until(lambda: visitor.closing)
            assert client.task.done()
            assert not closing.done() and not admission.closed and not closed
            grant.set()
            await until(releasing.is_set)
            assert not closing.done() and not visitor.session._closed and not closed
            assert not factory.bridges[0].calls and not visitor.ai_active
            released.set()
            await closing
            assert admission.released == [("permit-0", True)]
            assert visitor.session._closed and admission.closed and closed == ["source"]
            assert not app.sessions._cleanup and not factory.bridges[0].calls
        finally:
            grant.set()
            released.set()
            if closing is not None:
                await closing
            await asyncio.gather(client.task, return_exceptions=True)
            await app.close()
    asyncio.run(scenario())


def test_startup_timings_are_numeric_phases_without_capability_or_source_details():
    async def scenario():
        factory, admission, connection = Factory(), Admission(), Connection()
        manager = SessionManager(factory, admission, now=factory.clock)
        try:
            visitor, auth = await manager.attach({"type": "hello"}, "peer", connection)
            await manager.receive(visitor, connection, playback(visitor.session))
            phases = manager.snapshot(visitor, visitor.session.snapshot())["startup_phase_timings"]
            assert set(phases) == {"creation_admission_seconds", "factory_source_seconds",
                                   "director_admission_seconds"}
            assert all(isinstance(value, float) and value >= 0 for value in phases.values())
            encoded = json.dumps(phases)
            assert auth["session_id"] not in encoded and auth["resume_token"] not in encoded
            assert "synthetic.mp4" not in encoded
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [False, True])
def test_lifespan_requires_optional_financial_warmup_and_reports_only_static_failure(failure):
    async def scenario():
        factory, admission = Factory(), Admission()
        order, output = [], []
        class Source:
            def warm(self):
                order.append("source")
            def close(self):
                order.append("closed")
                return True
        async def warm():
            order.append("admission")
            if failure:
                raise RuntimeError("private-provider-detail")
        admission.warm = warm
        app = create_app(env={"MATCHPULSE_ALLOWED_ORIGINS": ORIGIN}, admission=admission,
            session_factory=factory, routes=Routes(), source_access=Source())
        incoming = asyncio.Queue()
        await incoming.put({"type": "lifespan.startup"})
        await incoming.put({"type": "lifespan.shutdown"})
        async def send(event):
            output.append(event)
        await app({"type": "lifespan"}, incoming.get, send)
        assert order == ["source", "admission", "closed"]
        if failure:
            assert output == [{"type": "lifespan.startup.failed",
                               "message": "Admission initialization unavailable"}]
            assert app._sweeper is None and app._closed
        else:
            assert output == [{"type": "lifespan.startup.complete"}, {"type": "lifespan.shutdown.complete"}]
        assert "private-provider-detail" not in json.dumps(output)
        assert admission.closed
    asyncio.run(scenario())
