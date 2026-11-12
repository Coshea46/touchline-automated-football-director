"""Owned Director teardown/reset leaves independent caption and media state intact."""

import asyncio
import threading
import time

import pytest

from touchline.ml_inference.models.release import NEW_MODEL_VERSION
from touchline.replay import ReplaySession, SessionConfig
from tests.support.captions.session import CaptionWorker
from tests.support.replay.session import Clock, FakeBridge, playback, registry


async def prepared_session(*, bridge=None, worker=None, bridge_factory=None):
    clock = Clock()
    prepared = SessionConfig(monitor=True, mode="real", model_version=NEW_MODEL_VERSION,
        checkpoint=NEW_MODEL_VERSION, max_duration_seconds=1200,
        max_estimated_cost_usd=2.20, max_restarts=0, gpu_workers=1)
    session = await ReplaySession(registry(), prepared_config=prepared, bridge=bridge,
        bridge_factory=bridge_factory, caption_worker=worker,
        caption_max_audio_seconds=1200, caption_max_cost_usd=.20,
        now=clock, monotonic=clock).initialize()
    session.connect("owner")
    session.receive("owner", playback(session, position=12))
    return session, prepared, clock


def running_owned(session, prepared):
    session.config = prepared
    session.director.mode, session.director.model_version = prepared.mode, prepared.model_version
    session.director.set_auto(True)
    session._handle = "owned-director"
    session._worker_ready = True
    session.state = "running"


def test_director_only_stop_preserves_live_captions_sources_and_single_caption_loop():
    async def scenario():
        worker, bridge = CaptionWorker(), FakeBridge()
        session, prepared, _ = await prepared_session(bridge=bridge, worker=worker)
        closed_sources = []
        original_close = session.registry.close
        session.registry.close = lambda: (closed_sources.append(True), original_close())[-1]
        try:
            running_owned(session, prepared)
            session.start()
            caption_loop = next(task for task in session._tasks if task.get_name() == "replay-captions")
            before = session.caption_state()
            source_objects = session.sources
            position, epoch = session.position, session.clock_epoch
            assert await session.stop_monitoring("director_off", stop_captions=False, preserve_prepared=True)
            assert session.caption_worker is worker and session._prepared_config is prepared
            assert session.caption_state() == before
            assert session.sources is source_objects and not closed_sources
            assert (session.position, session.clock_epoch) == (position, epoch)
            assert session._handle is None and not session.config.monitor
            assert not session.director.auto_enabled and session.metrics["director_stop_confirmed"]
            assert [call[1] for call in bridge.calls if call[0] == "stop"] == ["owned-director"]
            assert all(call[0] not in {"stop", "close"} for call in worker.calls)
            assert caption_loop in session._tasks and not caption_loop.done()
            session.start()
            assert len(session._tasks) == 3
            assert sum(task.get_name() == "replay-captions" for task in session._tasks) == 1
            assert sum(call[0] == "start" for call in worker.calls) == 1
            assert len(worker.updates) >= 1
        finally:
            await session.close()
        assert closed_sources == [True]
        assert sum(call[0] == "close" for call in worker.calls) == 1
    asyncio.run(scenario())


def test_reset_after_confirmed_stop_resets_only_director_run_and_never_starts_a_bridge():
    async def scenario():
        worker, bridge = CaptionWorker(), FakeBridge()
        factories = []
        session, prepared, clock = await prepared_session(bridge=bridge, worker=worker,
            bridge_factory=lambda: factories.append(True))
        try:
            running_owned(session, prepared)
            session._monitor_started_at = clock() - 90
            session._ready_at = clock() - 80
            session._reserved_compute_usd = 1.10
            session._restarts, session._retry_at = 2, clock() + 10
            await session.stop_monitoring("director_off", stop_captions=False, preserve_prepared=True)
            before = session.caption_state()
            native = (session.position, session.playing, session.rate, session.clock_epoch)
            calls = list(bridge.calls)
            session.reset_director_run()  # The caller must have durably admitted a new run.
            assert session._prepared_config is prepared
            assert (prepared.max_duration_seconds, prepared.max_estimated_cost_usd) == (1200, 2.20)
            assert not session.config.monitor and session.config.mode == "disabled"
            assert session.state == "disabled" and not session._finished and not session._worker_ready
            assert session._monitor_started_at is session._ready_at is None
            assert session._reserved_compute_usd == session.metrics["reserved_compute_usd"] == 0
            assert (session._restarts, session._retry_at) == (0, 0)
            assert session.caption_worker is worker and session.caption_state() == before
            assert (session.position, session.playing, session.rate, session.clock_epoch) == native
            assert bridge.calls == calls and not factories
            assert session.auto_start_status() == (True, "available")
            session.receive("owner", {"type": "auto", "session_id": session.session_id, "enabled": True})
            assert session._activation_requested and bridge.calls == calls
        finally:
            await session.close()
    asyncio.run(scenario())


def test_fresh_admitted_reset_keeps_startup_intent_without_constructing_bridge():
    async def scenario():
        factories = []
        session, prepared, _ = await prepared_session(bridge_factory=lambda: factories.append(True))
        try:
            session.director.set_auto(True)  # Hosted startup intent is not a GPU permit.
            before = session.caption_state()
            session.reset_director_run()
            assert session.director.auto_enabled and session.bridge is None and not factories
            assert session._handle is session._start_inflight is None
            assert not session._activation_requested and not session._start_authorized
            assert session.caption_state() == before and session._prepared_config is prepared
        finally:
            await session.close()
    asyncio.run(scenario())


def test_uncertain_director_stop_is_bounded_and_cannot_reset_its_allowance():
    async def scenario():
        release = threading.Event()
        class SlowStop(FakeBridge):
            transport = "websocket"
            def stop(self, session_id, handle):
                release.wait(2)
                return {"confirmed": True}
        bridge = SlowStop()
        session, prepared, _ = await prepared_session(bridge=bridge)
        # Keep the test independent of the service's normal multi-second bound.
        from dataclasses import replace
        session.config = replace(prepared, shutdown_rpc_seconds=.02)
        session._handle = "owned-director"
        session._reserved_compute_usd = .5
        before = time.perf_counter()
        try:
            assert not await session.stop_monitoring("director_off", stop_captions=False, preserve_prepared=True)
            assert time.perf_counter() - before < .5
            assert session.metrics["shutdown_cancel_status"] == "timeout"
            assert not session.metrics["director_stop_confirmed"]
            with pytest.raises(RuntimeError, match="confirmed idle cleanup"):
                session.reset_director_run()
            assert session._reserved_compute_usd == .5
        finally:
            release.set()
            await session.close()
    asyncio.run(scenario())


def test_legacy_stop_still_closes_captions_and_discards_preparation():
    async def scenario():
        worker = CaptionWorker()
        session, prepared, _ = await prepared_session(bridge=FakeBridge(), worker=worker)
        try:
            running_owned(session, prepared)
            session.start()
            assert await session.stop_monitoring("legacy_stop")
            assert session.caption_worker is None and session._prepared_config is None
            assert not session._caption_enabled and session._caption_reason == "legacy_stop"
            assert sum(call[0] == "close" for call in worker.calls) == 1
            assert all(task.get_name() != "replay-captions" for task in session._tasks)
            with pytest.raises(RuntimeError, match="prepared configuration"):
                session.reset_director_run()
        finally:
            await session.close()
    asyncio.run(scenario())


def test_retained_caption_loop_observes_replacement_worker_without_duplicate_loop():
    async def scenario():
        first, second = CaptionWorker(), CaptionWorker()
        session, prepared, _ = await prepared_session(bridge=FakeBridge(), worker=first)
        try:
            running_owned(session, prepared)
            session.start()
            await session.stop_monitoring("director_off", stop_captions=False, preserve_prepared=True)
            caption_loop = next(task for task in session._tasks if task.get_name() == "replay-captions")
            session.caption_worker = None
            await asyncio.sleep(.12)
            assert not caption_loop.done()
            # Hosted caption lifecycle owns closing/starting each replacement.
            first.close()
            second.start()
            session.caption_worker = second
            session.start()
            await asyncio.sleep(.12)
            assert second.updates
            assert next(task for task in session._tasks if task.get_name() == "replay-captions") is caption_loop
            assert sum(task.get_name() == "replay-captions" for task in session._tasks) == 1
        finally:
            await session.close()
    asyncio.run(scenario())
