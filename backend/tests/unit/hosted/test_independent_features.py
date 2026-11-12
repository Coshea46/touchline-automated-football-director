"""Independent paid controls, startup intent and per-run deadlines without providers."""

import asyncio

from touchline.hosted.sessions import SessionManager
from tests.support.hosted import Admission, Connection, Factory, until
from tests.support.replay.session import playback


async def toggle(manager, visitor, connection, kind, enabled):
    return await manager.receive(visitor, connection, {"type": kind,
        "session_id": visitor.session.session_id, "enabled": enabled})


def test_default_director_starts_once_after_ready_playback_and_other_visitors_run_in_parallel():
    async def scenario():
        factory, admission = Factory(), Admission()
        manager = SessionManager(factory, admission, now=factory.clock)
        a, b = Connection(), Connection()
        try:
            first, _ = await manager.attach({"type": "hello"}, "a", a)
            assert first.session.director.auto_enabled
            assert not first.session._caption_enabled and not admission.acquired
            assert first.session.bridge is None
            await manager.receive(first, a, playback(first.session, stalled=True, available=False))
            assert not admission.acquired
            await manager.receive(first, a, playback(first.session, position=1, available=True))
            await until(lambda: any(call[0] == "start" for call in factory.bridges[0].calls))
            await manager.receive(first, a, playback(first.session, position=2, available=True))
            second, _ = await manager.attach({"type": "hello"}, "b", b)
            await manager.receive(second, b, playback(second.session, available=True))
            await until(lambda: any(call[0] == "start" for call in factory.bridges[1].calls))
            assert first.ai_active and second.ai_active
            assert [row[2] for row in admission.acquired] == ["director", "director"]
            assert all(len([call for call in bridge.calls if call[0] == "start"]) == 1
                       for bridge in factory.bridges)
            assert manager.snapshot(first, first.session.snapshot())["ai_limit_seconds"] == 1200
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_caption_only_run_uses_no_gpu_and_caption_restarts_have_fresh_workers_and_permits():
    async def scenario():
        factory, admission, connection = Factory(), Admission(), Connection()
        manager = SessionManager(factory, admission, now=factory.clock)
        try:
            visitor, _ = await manager.attach({"type": "hello"}, "peer", connection)
            await toggle(manager, visitor, connection, "auto", False)
            await manager.receive(visitor, connection, playback(visitor.session, position=19, available=True))
            assert await toggle(manager, visitor, connection, "captions", True) is None
            worker = visitor.session.caption_worker
            await until(lambda: worker.updates)
            assert not visitor.ai_active and visitor.captions_active
            assert [row[2] for row in admission.acquired] == ["captions"]
            assert visitor.session.bridge is None and not factory.bridges[0].calls
            old_deadline = visitor.captions_deadline
            await toggle(manager, visitor, connection, "captions", True)
            assert len(admission.acquired) == 1 and visitor.captions_deadline == old_deadline
            await toggle(manager, visitor, connection, "captions", False)
            await visitor.captions_stop_task
            assert visitor.captions_cleanup_confirmed and admission.released == [("permit-0", True)]
            assert visitor.session.caption_worker is not worker and not visitor.session._caption_enabled
            factory.clock.value += 1
            assert await toggle(manager, visitor, connection, "captions", True) is None
            assert len(admission.acquired) == 2 and visitor.captions_deadline == old_deadline + 1
            assert visitor.session.position == 19 and visitor.session.bridge is None
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_each_off_control_preserves_the_other_feature_and_its_original_deadline():
    async def scenario():
        factory, admission, connection = Factory(), Admission(), Connection()
        manager = SessionManager(factory, admission, now=factory.clock)
        try:
            visitor, _ = await manager.attach({"type": "hello"}, "peer", connection)
            await manager.receive(visitor, connection, playback(visitor.session, position=5, available=True))
            await until(lambda: factory.bridges[0].calls)
            await toggle(manager, visitor, connection, "captions", True)
            worker, caption_deadline = visitor.session.caption_worker, visitor.captions_deadline
            await until(lambda: worker.updates)
            await toggle(manager, visitor, connection, "auto", False)
            await visitor.stop_task
            assert not visitor.ai_active and visitor.captions_active
            assert visitor.session.caption_worker is worker and visitor.session._caption_enabled
            assert visitor.captions_deadline == caption_deadline
            assert not any(call[0] == "close" for call in worker.calls)
            assert await toggle(manager, visitor, connection, "auto", True) is None
            await until(lambda: len([call for call in factory.bridges[0].calls if call[0] == "start"]) == 2)
            director_deadline = visitor.ai_deadline
            await toggle(manager, visitor, connection, "captions", False)
            await visitor.captions_stop_task
            assert visitor.ai_active and visitor.session.director.auto_enabled
            assert visitor.ai_deadline == director_deadline
            assert [row[2] for row in admission.acquired] == ["director", "captions", "director"]
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_twenty_minute_expiry_does_not_end_the_visitor_and_allows_a_new_run_without_cooldown():
    async def scenario():
        factory, admission, connection = Factory(), Admission(), Connection()
        manager = SessionManager(factory, admission, now=factory.clock)
        try:
            visitor, _ = await manager.attach({"type": "hello"}, "peer", connection)
            factory.clock.value += 100
            await manager.receive(visitor, connection, playback(visitor.session, position=45, available=True))
            deadline = visitor.ai_deadline
            assert visitor.expires_at >= deadline + manager.grace
            factory.clock.value = deadline - 1
            await manager.sweep()
            assert visitor.ai_active
            factory.clock.value = deadline
            await manager.sweep()
            await visitor.stop_task
            assert not visitor.ai_active and visitor.ai_cleanup_confirmed and not visitor.closing
            assert visitor.session.position == 45
            assert await toggle(manager, visitor, connection, "auto", True) is None
            assert visitor.ai_active and visitor.ai_deadline == deadline + 1200
            assert len(admission.acquired) == 2
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_caption_admission_denial_is_scoped_and_leaves_existing_director_running():
    async def scenario():
        factory, admission, connection = Factory(), Admission(), Connection()
        manager = SessionManager(factory, admission, now=factory.clock)
        try:
            visitor, _ = await manager.attach({"type": "hello"}, "peer", connection)
            await manager.receive(visitor, connection, playback(visitor.session, available=True))
            admission.denial = "ai_rate_limited"
            error = await toggle(manager, visitor, connection, "captions", True)
            assert error["code"] == "rate_limited" and error["feature"] == "captions"
            assert error["retry_after"] == 12
            assert visitor.ai_active and visitor.session.director.auto_enabled
            assert not visitor.captions_active and not visitor.session._caption_enabled
            assert visitor.ai_reason == "active"
        finally:
            await manager.close()
    asyncio.run(scenario())
