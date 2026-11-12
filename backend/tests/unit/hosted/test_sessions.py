"""Independent visitor clocks and capability ownership without cloud services."""

import asyncio

import pytest

from touchline.hosted.sessions import SessionDenied, SessionManager
from tests.support.hosted import Admission, Connection, Factory
from tests.support.replay.session import playback


def test_visitors_have_distinct_zero_clocks_and_cannot_control_another_session():
    async def scenario():
        factory, admission = Factory(), Admission()
        manager = SessionManager(factory, admission, now=factory.clock)
        a, b = Connection(), Connection()
        try:
            first, auth = await manager.attach({"type": "hello"}, "peer-a", a)
            assert first.session.director.auto_enabled and not first.ai_active
            await manager.receive(first, a, {"type": "auto", "session_id": auth["session_id"], "enabled": False})
            await manager.receive(first, a, playback(first.session, position=41))
            second, other = await manager.attach({"type": "hello"}, "peer-b", b)
            assert auth["session_id"] != other["session_id"]
            assert auth["resume_token"] != other["resume_token"]
            assert first.session.position == 41 and second.session.position == 0
            assert second.session.director.auto_enabled and not second.ai_active and not second.session._caption_enabled
            error = await manager.receive(second, b, playback(first.session, position=100))
            assert error["code"] == "session_mismatch" and first.session.position == 41
            first.session._publish_session()
            assert a.payloads and not b.payloads
            assert auth["resume_token"] not in repr(first)
            assert not admission.acquired and not factory.bridges[0].calls
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_resume_preserves_position_but_invalid_or_partial_capabilities_never_create_replacement():
    async def scenario():
        factory, admission = Factory(), Admission()
        manager = SessionManager(factory, admission, now=factory.clock)
        original, resumed = Connection(), Connection()
        try:
            visitor, auth = await manager.attach({"type": "hello"}, "peer-a", original)
            await manager.receive(visitor, original, {"type": "auto", "session_id": auth["session_id"], "enabled": False})
            await manager.receive(visitor, original, playback(visitor.session, position=57.5))
            manager.detach(visitor, original)
            for hello, code in [({"type": "hello", "session_id": auth["session_id"]}, "resume_rejected"),
                    ({"type": "hello", **{key: auth[key] for key in ("session_id", "resume_token")},
                      "resume_token": "x" * 43}, "resume_rejected"),
                    ({"type": "hello", "session_id": "0" * 32, "resume_token": "x" * 43}, "session_expired")]:
                with pytest.raises(SessionDenied, match=code):
                    await manager.attach(hello, "peer-b", Connection())
            restored, response = await manager.attach({"type": "hello", "session_id": auth["session_id"],
                "resume_token": auth["resume_token"]}, "peer-b", resumed)
            assert restored is visitor and restored.session.position == 57.5 and response["resumed"]
            assert len(factory.sessions) == 1 and admission.creations == ["peer-a"]
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_authenticated_takeover_fences_old_messages_and_old_disconnect_cleanup():
    async def scenario():
        factory, admission = Factory(), Admission()
        manager = SessionManager(factory, admission, now=factory.clock)
        old, new = Connection(), Connection()
        try:
            visitor, auth = await manager.attach({"type": "hello"}, "peer", old)
            await manager.attach({"type": "hello", "session_id": auth["session_id"],
                "resume_token": auth["resume_token"]}, "peer", new)
            manager.detach(visitor, old)
            assert visitor.connection is new and visitor.session.controller == new.client_id
            with pytest.raises(SessionDenied, match="session_replaced"):
                await manager.receive(visitor, old, playback(visitor.session, position=99))
            await manager.receive(visitor, new, playback(visitor.session, position=12))
            assert visitor.session.position == 12
            await asyncio.sleep(0)
            assert old.closed == ["session_replaced"]
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("age,disconnected", [(1200, False), (60, True)])
def test_hard_ttl_and_disconnect_grace_expire_without_silent_new_session(age, disconnected):
    async def scenario():
        factory, admission = Factory(), Admission()
        manager = SessionManager(factory, admission, now=factory.clock)
        connection = Connection()
        try:
            visitor, auth = await manager.attach({"type": "hello"}, "peer", connection)
            if disconnected:
                manager.detach(visitor, connection)
            factory.clock.value += age
            await manager.sweep()
            with pytest.raises(SessionDenied, match="session_expired"):
                await manager.attach({"type": "hello", "session_id": auth["session_id"],
                    "resume_token": auth["resume_token"]}, "peer", Connection())
            assert len(factory.sessions) == 1
        finally:
            await manager.close()
        assert visitor.session._closed and not manager.visitors
    asyncio.run(scenario())


def test_slow_creation_counts_against_capacity_without_blocking_other_owned_sessions():
    async def scenario():
        factory, admission = Factory(), Admission()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(session_id, guard):
            entered.set()
            await release.wait()
            return await factory(session_id, guard)
        manager = SessionManager(slow, admission, now=factory.clock, max_sessions=1)
        task = asyncio.create_task(manager.attach({"type": "hello"}, "one", Connection()))
        try:
            await entered.wait()
            with pytest.raises(SessionDenied, match="capacity_reached"):
                await manager.attach({"type": "hello"}, "two", Connection())
            release.set()
            await task
        finally:
            release.set()
            await manager.close()
    asyncio.run(scenario())


def test_failed_initialization_releases_pending_capacity_without_masking_original_error():
    async def scenario():
        admission = Admission()
        async def fail(*args):
            raise RuntimeError("synthetic initialization failure")
        manager = SessionManager(fail, admission, max_sessions=1)
        try:
            for _ in range(2):
                with pytest.raises(RuntimeError, match="synthetic initialization failure"):
                    await manager.attach({"type": "hello"}, "peer", Connection())
            assert manager._pending == 0 and not manager.visitors
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_takeover_while_message_admission_waits_fences_stale_ai_intent_before_reserving():
    async def scenario():
        factory, admission = Factory(), Admission()
        manager = SessionManager(factory, admission, now=factory.clock)
        entered, release = asyncio.Event(), asyncio.Event()
        old, new = Connection(), Connection()
        async def wait_for_ledger(_):
            entered.set()
            await release.wait()
        admission.allow_message = wait_for_ledger
        pending = None
        try:
            visitor, auth = await manager.attach({"type": "hello"}, "peer", old)
            pending = asyncio.create_task(manager.receive(visitor, old, {
                "type": "auto", "session_id": auth["session_id"], "enabled": True}))
            await entered.wait()
            await manager.attach({"type": "hello", "session_id": auth["session_id"],
                "resume_token": auth["resume_token"]}, "peer", new)
            release.set()
            with pytest.raises(SessionDenied, match="session_replaced"):
                await pending
            assert not admission.acquired and not visitor.ai_consumed
            assert visitor.connection is new and visitor.session.bridge is None
        finally:
            release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await manager.close()
    asyncio.run(scenario())


def test_shutdown_cancels_pending_visitor_creation_and_releases_its_capacity():
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def blocked_factory(*args):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        manager = SessionManager(blocked_factory, Admission())
        creation = asyncio.create_task(manager.attach({"type": "hello"}, "peer", Connection()))
        await entered.wait()
        await manager.close()
        assert creation.cancelled() and cancelled.is_set()
        assert manager._pending == 0 and not manager._creations and not manager.visitors
    asyncio.run(scenario())
