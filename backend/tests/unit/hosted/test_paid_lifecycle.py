"""Independent feature starts retain admission, fencing and owned cleanup."""

import asyncio
from dataclasses import replace

import pytest

from touchline.hosted.admission import AdmissionDenied
from touchline.hosted.sessions import SessionDenied, SessionManager
from tests.support.hosted import Admission, Connection, Factory, until
from tests.support.replay.session import playback


def synthetic_caption_source(visitor):
    # A synthetic identity lets the fake caption worker exercise lifecycle
    # controls. No media reader, provider client or network is involved.
    visitor.session.sources = tuple(replace(source, key="synthetic.mp4",
        identity={"digest": "a" * 64}) for source in visitor.session.sources)


async def toggle(manager, visitor, connection, feature, enabled):
    return await manager.receive(visitor, connection, {"type": feature,
        "session_id": visitor.session.session_id, "enabled": enabled})


@pytest.mark.parametrize("denial,code", [("ai_busy", "ai_busy"), ("ai_daily_limit", "budget_exhausted"),
    ("ai_rate_limited", "rate_limited"), ("admission_storage_unavailable", "server_unavailable"),
    ("ai_cleanup_pending", "ai_busy")])
def test_denied_component_admission_never_constructs_a_paid_bridge_or_enables_captions(denial, code):
    async def scenario():
        factory, admission, connection = Factory(), Admission(), Connection()
        admission.denial = denial
        manager = SessionManager(factory, admission, now=factory.clock)
        try:
            visitor, _ = await manager.attach({"type": "hello"}, "peer", connection)
            caption_error = await toggle(manager, visitor, connection, "captions", True)
            assert caption_error["code"] == code and caption_error["feature"] == "captions"
            # The default Director preference also requires durable admission
            # before the first factual playback report can create a bridge.
            error = await manager.receive(visitor, connection, playback(visitor.session))
            assert error["code"] == code and error["feature"] == "director" and error["retry_after"] == 12
            assert (await toggle(manager, visitor, connection, "auto", True))["code"] == code
            await visitor.session._control_tick()
            assert not admission.acquired and visitor.session.bridge is None
            assert not visitor.session._caption_enabled and not factory.bridges[0].calls
            assert not visitor.ai_consumed
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("finish", ["disconnect", "off", "deadline"])
def test_confirmed_director_end_preserves_manual_clock_and_allows_a_new_independently_reserved_run(finish):
    async def scenario():
        factory, admission, connection = Factory(), Admission(), Connection()
        manager = SessionManager(factory, admission, now=factory.clock)
        try:
            visitor, auth = await manager.attach({"type": "hello"}, "peer", connection)
            await manager.receive(visitor, connection, playback(visitor.session, position=19))
            assert await toggle(manager, visitor, connection, "auto", True) is None
            assert admission.acquired == [(auth["session_id"], "hashed-peer", "director")]
            await visitor.session._control_tick()
            assert len([call for call in factory.bridges[0].calls if call[0] == "start"]) == 1
            synthetic_caption_source(visitor)
            assert await toggle(manager, visitor, connection, "captions", True) is None
            worker, caption_deadline = visitor.session.caption_worker, visitor.session._caption_deadline
            assert visitor.captions_active and visitor.session._caption_enabled
            if finish == "disconnect":
                manager.detach(visitor, connection)
            elif finish == "off":
                await toggle(manager, visitor, connection, "auto", False)
            else:
                factory.clock.value += 1200
                await manager.sweep()
            assert not visitor.ai_active
            await visitor.stop_task
            assert visitor.session.position == 19 and not visitor.session._closed
            assert not visitor.session.config.monitor and visitor.session._prepared_config is not None
            if finish == "off":
                assert visitor.captions_active and visitor.session._caption_enabled
                assert visitor.session.caption_worker is worker
                assert visitor.session._caption_deadline == caption_deadline
                assert not [call for call in worker.calls if call[0] == "close"]
                assert admission.released == [("permit-0", True)]
            else:
                await visitor.captions_stop_task
                assert not visitor.captions_active and not visitor.session._caption_enabled
                assert set(admission.released) == {("permit-0", True), ("permit-1", True)}
                assert len([call for call in worker.calls if call[0] == "close"]) == 1
            if finish == "disconnect":
                connection = Connection()
                await manager.attach({"type": "hello", "session_id": auth["session_id"],
                    "resume_token": auth["resume_token"]}, "peer", connection)
            await manager.receive(visitor, connection, playback(visitor.session, position=28))
            assert await toggle(manager, visitor, connection, "auto", True) is None
            await visitor.session._control_tick()
            assert len([call for call in factory.bridges[0].calls if call[0] == "start"]) == 2
            assert admission.acquired[-1] == (auth["session_id"], "hashed-peer", "director")
            assert len(admission.acquired) == 3 and visitor.session.position == 28
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_unconfirmed_caption_cleanup_fences_only_its_component_and_keeps_director_independent():
    async def scenario():
        factory, admission, connection = Factory(), Admission(), Connection()
        manager = SessionManager(factory, admission, now=factory.clock)
        try:
            visitor, auth = await manager.attach({"type": "hello"}, "peer", connection)
            await toggle(manager, visitor, connection, "auto", False)
            synthetic_caption_source(visitor)
            await manager.receive(visitor, connection, playback(visitor.session, position=8))
            worker = visitor.session.caption_worker
            worker.close = lambda: False
            assert await toggle(manager, visitor, connection, "captions", True) is None
            manager.detach(visitor, connection)
            await visitor.captions_stop_task
            assert admission.released == [("permit-0", False)]
            assert visitor.session.metrics["caption_stop_confirmed"] is False
            connection = Connection()
            await manager.attach({"type": "hello", "session_id": auth["session_id"],
                "resume_token": auth["resume_token"]}, "peer", connection)
            assert (await toggle(manager, visitor, connection, "captions", True))["code"] == "ai_busy"
            assert await toggle(manager, visitor, connection, "auto", True) is None
            assert [row[2] for row in admission.acquired] == ["captions", "director"]
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_real_admission_keeps_failed_visitor_fenced_without_blocking_another_visitor(tmp_path):
    from touchline.hosted.admission import AdmissionPolicy
    async def scenario():
        factory = Factory()
        admission = AdmissionPolicy(tmp_path / "admission.sqlite3", now=factory.clock,
            monotonic=factory.clock, env={"TOUCHLINE_AI_DAILY_BUDGET_USD": "100",
                                         "TOUCHLINE_AI_TOTAL_BUDGET_USD": "100"})
        manager = SessionManager(factory, admission, now=factory.clock)
        a, b = Connection(), Connection()
        first = second = None
        try:
            first, _ = await manager.attach({"type": "hello"}, "peer-a", a)
            await manager.receive(first, a, playback(first.session, position=8))
            await first.session._control_tick()
            assert first.session._handle is not None
            def failed_stop(*args):
                raise RuntimeError("Synthetic owned stop is unconfirmed")
            factory.bridges[0].stop = failed_stop
            await toggle(manager, first, a, "auto", False)
            await first.stop_task
            assert not first.ai_cleanup_confirmed
            assert (await toggle(manager, first, a, "auto", True))["code"] == "ai_busy"
            second, _ = await manager.attach({"type": "hello"}, "peer-b", b)
            assert await manager.receive(second, b, playback(second.session, position=5)) is None
            await second.session._control_tick()
            assert second.ai_active and second.session._handle is not None
            with admission._connect() as db:
                closed = dict(db.execute("SELECT session,closed FROM permits"))
                assert closed[first.ai_permit] == closed[second.ai_permit] == 0
                assert db.execute("SELECT SUM(amount) FROM permits").fetchone()[0] == pytest.approx(4.40)
        finally:
            await manager.close()
        with admission._connect() as db:
            closed = dict(db.execute("SELECT session,closed FROM permits"))
            assert closed[first.ai_permit] == 0 and closed[second.ai_permit] == 1
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


def test_delayed_denial_from_replaced_socket_cannot_mutate_new_owner_director_intent_or_reason():
    async def scenario():
        factory, admission = Factory(), Admission()
        manager = SessionManager(factory, admission, now=factory.clock)
        old, new = Connection(), Connection()
        entered, release = asyncio.Event(), asyncio.Event()
        original_acquire, pending = admission.acquire_ai, None
        async def acquire(visitor_id, peer, *, component="director"):
            if component == "director" and not entered.is_set():
                entered.set()
                await release.wait()
                raise AdmissionDenied("ai_budget_exhausted", 12)
            return await original_acquire(visitor_id, peer, component=component)
        admission.acquire_ai = acquire
        try:
            visitor, auth = await manager.attach({"type": "hello"}, "peer", old)
            pending = asyncio.create_task(toggle(manager, visitor, old, "auto", True))
            await entered.wait()
            await manager.attach({"type": "hello", "session_id": auth["session_id"],
                "resume_token": auth["resume_token"]}, "peer", new)
            assert await toggle(manager, visitor, new, "auto", True) is None
            expected = (visitor.session.director.auto_enabled, visitor.ai_active,
                        visitor.ai_reason, visitor.ai_permit)
            assert expected[:3] == (True, True, "active")
            release.set()
            with pytest.raises(SessionDenied, match="session_replaced"):
                await pending
            assert visitor.connection is new and visitor.session.controller == new.client_id
            assert (visitor.session.director.auto_enabled, visitor.ai_active,
                    visitor.ai_reason, visitor.ai_permit) == expected
            assert admission.released == []
            assert len(admission.acquired) == 1
        finally:
            release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await manager.close()
    asyncio.run(scenario())


def test_delayed_success_from_replaced_socket_retires_only_its_director_permit_and_preserves_new_captions():
    async def scenario():
        factory, admission = Factory(), Admission()
        manager = SessionManager(factory, admission, now=factory.clock)
        old, new = Connection(), Connection()
        entered, release = asyncio.Event(), asyncio.Event()
        original_acquire, pending = admission.acquire_ai, None
        async def acquire(visitor_id, peer, *, component="director"):
            if component == "director":
                entered.set()
                await release.wait()
            return await original_acquire(visitor_id, peer, component=component)
        admission.acquire_ai = acquire
        try:
            visitor, auth = await manager.attach({"type": "hello"}, "peer", old)
            pending = asyncio.create_task(toggle(manager, visitor, old, "auto", True))
            await entered.wait()
            await manager.attach({"type": "hello", "session_id": auth["session_id"],
                "resume_token": auth["resume_token"]}, "peer", new)
            synthetic_caption_source(visitor)
            await manager.receive(visitor, new, playback(visitor.session, position=8))
            assert await toggle(manager, visitor, new, "captions", True) is None
            worker, deadline, generation = (visitor.session.caption_worker,
                visitor.session._caption_deadline, visitor.session._caption_generation)
            assert visitor.captions_active and visitor.session._caption_enabled
            assert visitor.captions_permit == "permit-0"
            release.set()
            with pytest.raises(SessionDenied, match="session_replaced"):
                await pending
            await visitor.stop_task
            assert visitor.connection is new and not visitor.ai_active
            assert visitor.captions_active and visitor.session._caption_enabled
            assert visitor.session.caption_worker is worker
            assert (visitor.session._caption_deadline, visitor.session._caption_generation) == (deadline, generation)
            assert not [call for call in worker.calls if call[0] == "close"]
            assert admission.released == [("permit-1", True)]
            assert [row[2] for row in admission.acquired] == ["captions", "director"]
        finally:
            release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await manager.close()
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
            await until(entered.is_set)
            manager.detach(visitor, connection)
            await asyncio.sleep(.01)
            assert not visitor.ai_active and not admission.released
            release.set()
            await visitor.stop_task
            assert [call[1] for call in bridge.calls if call[0] == "stop"] == ["own-handle"]
            assert admission.released == [("permit-0", True)]
            assert visitor.session.position == 8 and not visitor.session._closed
        finally:
            release.set()
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("advance,ttl,expected", [(1200, 1200, "session_expired"), (1201, 2400, "ai_finished")])
def test_slow_durable_admission_cannot_authorize_after_visitor_or_original_lease_deadline(advance, ttl, expected):
    async def scenario():
        from touchline.hosted.sessions import SessionDenied
        factory, admission, connection = Factory(), Admission(), Connection()
        manager = SessionManager(factory, admission, now=factory.clock, ttl_seconds=ttl)
        try:
            visitor, auth = await manager.attach({"type": "hello"}, "peer", connection)
            async def slow_commit(session_id, peer, *, component="director"):
                original_deadline = factory.clock.value + 1200
                factory.clock.value += advance
                return {"permit_id": "slow-permit", "duration_seconds": 1200, "expires_at": original_deadline}
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
            assert admission.released == [("slow-permit", True)]
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
