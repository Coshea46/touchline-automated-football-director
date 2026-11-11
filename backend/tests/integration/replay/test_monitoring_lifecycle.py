"""Offline tests for integration replay test_monitoring_lifecycle."""


import asyncio
import threading
import time
from touchline.replay import ReplaySession
from tests.support.replay.session import Clock, FakeBridge, registry, configuration, scores, playback


def test_control_and_score_rpc_cadence_subtracts_network_time_without_overlapping_calls():
    class SlowBridge(FakeBridge):
        def __init__(self):
            super().__init__()
            self.starts = {"control": [], "score": []}
            self.active = {"control": 0, "score": 0}
            self.overlapped = False

        def rpc(self, kind, duration):
            self.starts[kind].append(time.perf_counter())
            self.active[kind] += 1
            self.overlapped |= self.active[kind] > 1
            time.sleep(duration)
            self.active[kind] -= 1

        def update_control(self, session_id, control):
            self.rpc("control", 0.08)

        def latest(self, session_id):
            self.rpc("score", 0.04)
            return {"produced_at": time.time() - 0.1, "scores": []}

    async def scenario():
        bridge = SlowBridge()
        session = await ReplaySession(registry(), config=configuration(control_seconds=0.12, poll_seconds=0.06),
                                       bridge=bridge).initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session))
        session._handle = "already-started"
        session.start()
        for _ in range(200):
            if len(bridge.starts["control"]) >= 5 and len(bridge.starts["score"]) >= 8:
                break
            await asyncio.sleep(0.01)
        # Gracefully finish the final in-flight reads/writes before teardown.
        session._closing = True
        await asyncio.gather(*session._tasks)
        for kind, ceiling in (("control", 0.17), ("score", 0.09)):
            starts = bridge.starts[kind]
            assert len(starts) >= 5
            average_interval = (starts[-1] - starts[0]) / (len(starts) - 1)
            assert average_interval < ceiling
        assert not bridge.overlapped
        assert session.metrics["control_rpc_count"] >= 5
        assert session.metrics["score_rpc_count"] >= 8
        assert session.metrics["control_rpc_seconds"] >= 5 * 0.08
        assert session.metrics["score_rpc_seconds"] >= 8 * 0.04
        # Epoch-sized timestamps lose sub-microsecond precision on subtraction.
        assert session.metrics["score_age_at_read_seconds"] >= 0.1 - 1e-6
        assert session.metrics["score_read_completed_at"] > 0
        await session.close()

    asyncio.run(scenario())


def test_session_start_ready_scores_shutdown_and_sdk_calls_off_event_loop():
    async def scenario():
        clock = Clock()
        bridge = FakeBridge()
        sent = []
        session = await ReplaySession(registry(), config=configuration(), bridge=bridge,
                                       publish=sent.append, now=clock, monotonic=clock,
                                       session_id="session").initialize()
        session.connect("viewer")
        # Startup buffer reports paused at zero and still starts the worker.
        session.receive("viewer", playback(session, playing=False))
        await session._control_tick()
        assert session.state == "starting"
        assert [call[0] for call in bridge.calls] == ["control", "start"]
        assert bridge.calls[0][1]["position"] == 0
        assert bridge.calls[1][1]["matches"][1]["offset_seconds"] == 20
        ready = scores()
        ready.update(state="ready", scores=[])
        assert not session.accept_snapshot(ready)
        assert session.state == "running"
        session.receive("viewer", playback(session, position=5))
        # Five seconds later is normal motion rather than an instantaneous seek.
        session.clock_epoch = 0
        session.director.reset(0)
        assert session.accept_snapshot(scores(sequence=2))
        rec = sent[-1]
        assert rec["type"] == "recommendation" and rec["selected_match_id"] == "cam-2"
        assert rec["score_kind"] == "mock" and rec["valid_until"] == 1003
        # A published proposal isn't committed until the actual viewer accepts.
        assert session.director.current_match_id == "cam-1"
        assert session.receive("viewer", playback(session, match_id="cam-1", position=5)) is None
        assert session.receive("viewer", {"type": "ack", "session_id": "session",
                                          "sequence": rec["sequence"], "clock_epoch": 0,
                                          "match_id": "cam-2"}) is None
        assert session.director.current_match_id == "cam-2"
        assert session.metrics["last_ack"]["observation_to_ack_seconds"] == 0.2
        await session.close()
        assert [call[0] for call in bridge.calls][-2:] == ["control", "stop"]
        assert not bridge.calls[-2][1]["active"]
        assert all(call[2] != threading.get_ident() for call in bridge.calls)

    asyncio.run(scenario())


def test_shutdown_during_slow_submission_recovers_and_cancels_owned_handle():
    async def scenario():
        gate = threading.Event()

        class SlowBridge(FakeBridge):
            def start(self, spec):
                gate.wait(2)
                return super().start(spec)

        bridge = SlowBridge()
        session = await ReplaySession(registry(), config=configuration(), bridge=bridge).initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session))
        session.start()
        for _ in range(100):
            if session._start_inflight is not None:
                break
            await asyncio.sleep(0.001)
        closing = asyncio.create_task(session.close())
        await asyncio.sleep(0.01)
        gate.set()
        await closing
        assert session._handle is None
        assert any(call[0] == "stop" and call[1] == "own-handle" for call in bridge.calls)

    asyncio.run(scenario())


def test_shutdown_bounds_blocked_rpc_and_reports_unconfirmed_cancellation():
    async def scenario():
        release = threading.Event()

        class SlowStop(FakeBridge):
            def stop(self, session_id, handle):
                release.wait(2)

        session = await ReplaySession(registry(), config=configuration(shutdown_rpc_seconds=0.02),
                                       bridge=SlowStop()).initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session))
        session._handle = "owned-handle"
        before = time.perf_counter()
        try:
            await session.close()
            assert time.perf_counter() - before < 0.5
            assert session.metrics["shutdown_cancel_status"] == "timeout"
            assert not session.metrics["worker_stop_succeeded"]
            assert session.metrics["shutdown_sdk_thread_may_remain"]
            elapsed = session.metrics["shutdown_total_seconds"]
            await session.close()  # Idempotent and preserves the actual teardown timings.
            assert session.metrics["shutdown_total_seconds"] == elapsed
        finally:
            release.set()

    asyncio.run(scenario())
