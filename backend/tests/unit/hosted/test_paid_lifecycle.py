"""Paid intent requires durable admission and expires without reconnect renewal."""

import asyncio

import pytest

from touchline.hosted.sessions import SessionManager
from tests.support.hosted import Admission, Connection, Factory
from tests.support.hosted import until
from tests.support.replay.session import playback


@pytest.mark.parametrize("denial,code", [("ai_busy", "ai_busy"),
    ("ai_daily_limit", "budget_exhausted"), ("ai_rate_limited", "rate_limited"),
    ("admission_storage_unavailable", "server_unavailable"), ("ai_cleanup_pending", "ai_busy")])
def test_denied_admission_never_enables_captions_or_constructs_inference_bridge(denial, code):
    async def scenario():
        factory, admission, connection = Factory(), Admission(), Connection()
        admission.denial = denial
        manager = SessionManager(factory, admission, now=factory.clock)
        try:
            visitor, _ = await manager.attach({"type": "hello"}, "peer", connection)
            assert (await manager.receive(visitor, connection, {
                "type": "captions", "session_id": visitor.session.session_id,
                "enabled": True}))["code"] == "ai_required"
            await manager.receive(visitor, connection, playback(visitor.session))
            error = await manager.receive(visitor, connection, {
                "type": "auto", "session_id": visitor.session.session_id, "enabled": True})
            assert error["code"] == code and error["retry_after"] == 12
            await visitor.session._control_tick()
            assert not admission.acquired and visitor.session.bridge is None
            assert not visitor.session._caption_enabled and not factory.bridges[0].calls
            assert not visitor.ai_consumed
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("finish", ["disconnect", "off", "deadline"])
def test_one_permit_ends_gpu_and_captions_without_resetting_manual_clock_or_renewing(finish):
    async def scenario():
        factory, admission, connection = Factory(), Admission(), Connection()
        manager = SessionManager(factory, admission, now=factory.clock)
        try:
            visitor, auth = await manager.attach({"type": "hello"}, "peer", connection)
            await manager.receive(visitor, connection, playback(visitor.session, position=19))
            assert await manager.receive(visitor, connection, {
                "type": "auto", "session_id": auth["session_id"], "enabled": True}) is None
            assert admission.acquired == [(auth["session_id"], "hashed-peer")]
            await visitor.session._control_tick()
            assert len([call for call in factory.bridges[0].calls if call[0] == "start"]) == 1
            if finish == "disconnect":
                manager.detach(visitor, connection)
            elif finish == "off":
                await manager.receive(visitor, connection, {
                    "type": "auto", "session_id": auth["session_id"], "enabled": False})
            else:
                factory.clock.value += 120
                await manager.sweep()
            assert not visitor.ai_active and not visitor.session._caption_enabled
            await visitor.stop_task
            assert admission.released == [(auth["session_id"], True)]
            assert visitor.session.position == 19 and not visitor.session._closed
            assert not visitor.session.config.monitor and visitor.session.caption_worker is None
            assert len([call for call in factory.workers[0].calls if call[0] == "close"]) == 1
            if finish == "disconnect":
                connection = Connection()
                await manager.attach({"type": "hello", "session_id": auth["session_id"],
                    "resume_token": auth["resume_token"]}, "peer", connection)
            error = await manager.receive(visitor, connection, {
                "type": "auto", "session_id": auth["session_id"], "enabled": True})
            assert error["code"] == "ai_finished" and len(admission.acquired) == 1
            await manager.receive(visitor, connection, playback(visitor.session, position=28))
            assert visitor.session.position == 28
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_unconfirmed_caption_cleanup_never_releases_global_paid_ownership():
    async def scenario():
        factory, admission, connection = Factory(), Admission(), Connection()
        manager = SessionManager(factory, admission, now=factory.clock)
        try:
            visitor, auth = await manager.attach({"type": "hello"}, "peer", connection)
            factory.workers[0].close = lambda: False
            await manager.receive(visitor, connection, {
                "type": "auto", "session_id": auth["session_id"], "enabled": True})
            manager.detach(visitor, connection)
            await visitor.stop_task
            assert admission.released == [(auth["session_id"], False)]
            assert visitor.session.metrics["caption_stop_confirmed"] is False
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_core_guard_rechecks_admission_before_constructing_any_paid_bridge():
    async def scenario():
        factory = Factory()
        async def denied():
            return "ai_busy"
        session = await factory("synthetic-session", denied)
        session.connect("owner")
        try:
            session.receive("owner", playback(session))
            session.receive("owner", {"type": "auto", "session_id": session.session_id, "enabled": True})
            await session._control_tick()
            assert session.bridge is None and not factory.bridges[0].calls
            assert session.error_code == "ai_busy" and not session.director.auto_enabled
        finally:
            await session.close()
    asyncio.run(scenario())


def test_disconnect_during_native_submission_keeps_reservation_until_owned_handle_is_stopped():
    async def scenario():
        from threading import Event
        factory, admission, connection = Factory(), Admission(), Connection()
        manager = SessionManager(factory, admission, now=factory.clock)
        entered, release = Event(), Event()
        try:
            visitor, auth = await manager.attach({"type": "hello"}, "peer", connection)
            bridge = factory.bridges[0]
            original_start = bridge.start
            def blocked_start(spec):
                entered.set()
                assert release.wait(3)
                return original_start(spec)
            bridge.start = blocked_start
            await manager.receive(visitor, connection, playback(visitor.session, position=8))
            await manager.receive(visitor, connection, {
                "type": "auto", "session_id": auth["session_id"], "enabled": True})
            await until(entered.is_set)
            manager.detach(visitor, connection)
            await asyncio.sleep(.01)
            assert not visitor.ai_active and not admission.released
            release.set()
            await visitor.stop_task
            assert [call[1] for call in bridge.calls if call[0] == "stop"] == ["own-handle"]
            assert admission.released == [(auth["session_id"], True)]
            assert visitor.session.position == 8 and not visitor.session._closed
        finally:
            release.set()
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("advance,expected", [(1200, "session_expired"), (121, "ai_finished")])
def test_slow_durable_admission_cannot_authorize_after_visitor_or_original_lease_deadline(advance, expected):
    async def scenario():
        from touchline.hosted.sessions import SessionDenied
        factory, admission, connection = Factory(), Admission(), Connection()
        manager = SessionManager(factory, admission, now=factory.clock)
        try:
            visitor, auth = await manager.attach({"type": "hello"}, "peer", connection)
            async def slow_commit(session_id, peer):
                original_deadline = factory.clock.value + 120
                factory.clock.value += advance
                return {"duration_seconds": 120, "expires_at": original_deadline}
            admission.acquire_ai = slow_commit
            message = {"type": "auto", "session_id": auth["session_id"], "enabled": True}
            if expected == "session_expired":
                with pytest.raises(SessionDenied, match=expected):
                    await manager.receive(visitor, connection, message)
            else:
                assert (await manager.receive(visitor, connection, message))["code"] == expected
            assert visitor.ai_consumed and not visitor.ai_active
            assert not visitor.session._caption_enabled and visitor.session.bridge is None
            await visitor.stop_task
            assert admission.released == [(auth["session_id"], True)]
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_completed_native_submission_overtaken_by_cancellation_is_drained_and_stopped(monkeypatch):
    async def scenario():
        factory = Factory()
        async def allowed():
            return None
        session = await factory("synthetic-session", allowed)
        session.connect("owner")
        session.receive("owner", playback(session))
        session.config = session._prepared_config
        session.bridge = factory.bridges[0]
        session.director.set_auto(True)
        session._start_authorized = True
        async def completed_before_cancel(task):
            await task
            assert task.done() and task.result() == "own-handle"
            raise asyncio.CancelledError
        try:
            with monkeypatch.context() as scoped:
                scoped.setattr("touchline.replay.monitoring.asyncio.shield", completed_before_cancel)
                with pytest.raises(asyncio.CancelledError):
                    await session._start_worker()
            assert session._start_inflight is not None and session._start_inflight.done()
            assert await session.stop_monitoring("viewer_disconnected") is True
            assert [call[1] for call in factory.bridges[0].calls if call[0] == "stop"] == ["own-handle"]
        finally:
            await session.close()
    asyncio.run(scenario())
