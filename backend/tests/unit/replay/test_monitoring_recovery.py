"""Offline tests for unit replay test_monitoring_recovery."""


import asyncio
import json
import time
import pytest
from touchline.replay import ReplaySession
from tests.support.replay.session import Clock, FakeBridge, registry, configuration, scores, playback


def test_worker_start_failure_has_bounded_retries():
    async def scenario():
        clock = Clock()
        bridge = FakeBridge()
        bridge.error = RuntimeError("do-not-log-a-secret-URL")
        session = await ReplaySession(registry(), config=configuration(max_restarts=1), bridge=bridge,
                                       now=clock, monotonic=clock).initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session))
        # Exercise the background handler that records failures and retry delay.
        session.start()
        await asyncio.sleep(0.1)
        assert session.state == "error"
        assert session._restarts == 1
        assert session.snapshot()["error_code"] == "inference_unavailable"
        await session.close()

    asyncio.run(scenario())


def test_restart_cost_reservations_enforce_whole_session_budget():
    async def scenario():
        bridge = FakeBridge()
        clock = Clock()
        session = await ReplaySession(registry(), config=configuration(max_estimated_cost_usd=0.25),
                                       bridge=bridge, now=clock, monotonic=clock).initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session))
        await session._control_tick()
        assert session._handle is not None
        from touchline.ml_inference.streaming.contract import MAX_WORKER_RATE

        assert session.metrics["reserved_compute_usd"] == pytest.approx((300 + 330) * MAX_WORKER_RATE)
        session._stop_requested = True
        await session._control_tick()
        assert session._handle is None and session._finished
        assert session.error_code == "session_budget_reached"
        assert len([call for call in bridge.calls if call[0] == "start"]) == 1
        await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["session_limit", "all_sources_ended"])
def test_terminal_worker_snapshot_stops_instead_of_spawning_forever(reason):
    async def scenario():
        bridge = FakeBridge()
        session = await ReplaySession(registry(), config=configuration(), bridge=bridge,
                                       session_id="session").initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session))
        await session._control_tick()
        terminal = scores(now=time.time())
        terminal.update(state="stopped", reason=reason, scores=[])
        assert not session.accept_snapshot(terminal)
        await session._control_tick()
        assert session._finished and session._handle is None
        await session._control_tick()
        assert len([call for call in bridge.calls if call[0] == "start"]) == 1
        await session.close()

    asyncio.run(scenario())


def test_worker_startup_diagnostics_survive_error_handling_without_source_or_exception_text():
    from touchline.replay.diagnostics import worker_diagnostics

    async def scenario():
        session = await ReplaySession(registry(), config=configuration(), bridge=FakeBridge(),
                                       session_id="session").initialize()
        snapshot = scores()
        snapshot.update(state="error", reason="shard_error", error_type="ValueError",
                        error_phase="checkpoint", error="https://private.example/?token=secret",
                        shard_errors={"0": {"state": "error", "reason": "checkpoint_unavailable",
                                            "error_type": "ValueError", "error_phase": "checkpoint",
                                            "message": "private secret exception text"},
                                      "1": {"state": "error", "reason": "https://secret.example",
                                            "error_type": "ValueError: token=secret",
                                            "error_phase": ["secret"]}, "other-secret-shard": {}})
        assert not session.accept_snapshot(snapshot)
        assert session.metrics["worker_last_state"] == "error"
        assert session.metrics["worker_reason"] == "shard_error"
        assert session.metrics["worker_error_type"] == "ValueError"
        assert session.metrics["worker_shard_errors"]["0"]["reason"] == "checkpoint_unavailable"
        assert session.metrics["worker_shard_errors"]["1"]["reason"] is None
        assert "secret" not in json.dumps(session.metrics)
        assert "message" not in json.dumps(worker_diagnostics(snapshot))
        assert len(session.metrics["worker_shard_errors"]) == 2
        await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("match_count,workers", [(2, 1), (7, 2), (7, 4)])
def test_transport_loss_recovers_once_with_new_epoch_and_shared_reserved_budget(match_count, workers):
    async def scenario():
        clock, bridge = Clock(), FakeBridge()
        session = await ReplaySession(registry(match_count),
                                       config=configuration(gpu_workers=workers, max_restarts=1,
                                                            max_estimated_cost_usd=2 if workers == 4 else 1), bridge=bridge,
                                       now=clock, monotonic=clock, session_id="session").initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session))
        await session._control_tick()

        def failure(epoch):
            result = scores(epoch=epoch)
            result.update(state="error", scores=[], health={}, error_type="ConnectionClosedError",
                          reason="processing_error", error_phase="control")
            if workers > 1:
                result.update(reason="shard_error", error_type=None, error_phase=None,
                              shard_errors={str(workers - 1): {"reason": "processing_error",
                                                  "error_type": "ConnectionClosedError", "error_phase": "control"}})
            return result

        error = failure(0)
        assert not session.accept_snapshot(error)
        assert not session._finished and session._restarts == 1
        assert session.error_code == "inference_unavailable"
        assert not session.accept_snapshot(error) and session._restarts == 1
        await session._control_tick()
        assert session._handle is None and session.clock_epoch == 1
        assert not session.accept_snapshot(error)  # Previous socket cannot revive its old clock.
        clock.value += 4.9
        session.receive("viewer", playback(session, position=4.9))
        await session._control_tick()
        assert len([call for call in bridge.calls if call[0] == "start"]) == 1
        clock.value += 0.2
        session.receive("viewer", playback(session, position=5.1))
        await session._control_tick()
        starts = [call[1] for call in bridge.calls if call[0] == "start"]
        assert len(starts) == 2 and starts[-1]["clock_epoch"] == 1
        assert starts[-1]["start_position"] == 5.1 and starts[-1]["gpu_workers"] == workers
        assert 0 < session.metrics["reserved_compute_usd"] <= session.config.max_estimated_cost_usd
        assert starts[-1]["max_estimated_cost_usd"] < starts[0]["max_estimated_cost_usd"]
        session.accept_snapshot(failure(1))
        await session._control_tick()
        clock.value += 6
        session.receive("viewer", playback(session, position=11.1))
        await session._control_tick()
        assert session._restarts == 2
        assert len([call for call in bridge.calls if call[0] == "start"]) == 2
        assert session.metrics["worker_stop_succeeded"]
        await session.close()

    asyncio.run(scenario())


def test_two_shard_transport_recovery_cannot_exceed_aggregate_allowance():
    async def scenario():
        clock, bridge = Clock(), FakeBridge()
        session = await ReplaySession(registry(7), config=configuration(gpu_workers=2, max_estimated_cost_usd=0.5),
                                       bridge=bridge, now=clock, monotonic=clock, session_id="session").initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session))
        await session._control_tick()
        error = scores()
        error.update(state="error", scores=[], health={}, reason="shard_error",
                     shard_errors={"0": {"reason": "processing_error", "error_phase": "publication",
                                          "error_type": "TimeoutError"}})
        session.accept_snapshot(error)
        await session._control_tick()
        assert not session._finished
        clock.value += 6
        session.receive("viewer", playback(session, position=6))
        await session._control_tick()
        assert session._finished and session.error_code == "session_budget_reached"
        assert len([call for call in bridge.calls if call[0] == "start"]) == 1
        assert session.metrics["reserved_compute_usd"] < 0.5
        await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("ended", [True, False])
def test_seek_restarts_closed_eof_shard_group_but_keeps_active_shards_on_normal_seek(ended):
    async def scenario():
        clock, bridge = Clock(), FakeBridge()
        session = await ReplaySession(registry(), config=configuration(), bridge=bridge,
                                       now=clock, monotonic=clock, session_id="session").initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session, position=10))
        await session._control_tick()
        session._worker_ready = True
        session._set_state("running")
        session.worker_health = {"cam-1": {"status": "healthy"},
                                  "cam-2": {"status": "eof" if ended else "healthy"}}
        clock.value += 0.2
        session.receive("viewer", playback(session, position=1))
        assert session.clock_epoch == 1
        assert session._stop_requested is ended
        if ended:
            assert session.state == "starting" and not session._worker_ready
        await session._control_tick()
        starts = [call[1] for call in bridge.calls if call[0] == "start"]
        assert len(starts) == (2 if ended else 1)
        assert session.clock_epoch == 1  # The seek already invalidated the old epoch.
        if ended:
            assert starts[-1]["start_position"] == 1 and starts[-1]["clock_epoch"] == 1
            assert any(call[0] == "stop" for call in bridge.calls)
            assert starts[-1]["max_estimated_cost_usd"] < starts[0]["max_estimated_cost_usd"]
        stale_eof = scores(epoch=0, sequence=99)
        stale_eof.update(state="stopped", reason="all_sources_ended", scores=[], health={"cam-2": "eof"})
        assert not session.accept_snapshot(stale_eof)
        assert not session._finished and session.clock_epoch == 1
        await session.close()

    asyncio.run(scenario())


def test_partial_eof_seek_restart_uses_existing_allowance_instead_of_creating_budget():
    async def scenario():
        bridge = FakeBridge()
        session = await ReplaySession(registry(), config=configuration(max_estimated_cost_usd=0.25), bridge=bridge,
                                       session_id="session").initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session, position=10))
        await session._control_tick()
        session.worker_health = {"cam-2": {"status": "eof"}}
        session.receive("viewer", playback(session, position=0))
        await session._control_tick()
        assert session.error_code == "session_budget_reached" and session._finished
        assert session._handle is None
        assert len([call for call in bridge.calls if call[0] == "start"]) == 1
        assert session.metrics["reserved_compute_usd"] < 0.25
        await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [
    {"reason": "checkpoint_unavailable", "error_phase": "load"},
    {"reason": "processing_error", "error_phase": "reader"},
    {"reason": "shard_error", "shard_errors": {
        "0": {"reason": "processing_error", "error_phase": "control"},
        "1": {"reason": "checkpoint_unavailable", "error_phase": "validate"}}},
])
def test_checkpoint_or_non_transport_failures_remain_terminal(error):
    async def scenario():
        bridge = FakeBridge()
        session = await ReplaySession(registry(), config=configuration(), bridge=bridge,
                                       session_id="session").initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session))
        await session._control_tick()
        snapshot = scores()
        snapshot.update(state="error", scores=[], **error)
        session.accept_snapshot(snapshot)
        assert session._finished and session._restarts == 0 and session.error_code == "worker_error"
        await session._control_tick()
        assert session._handle is None
        assert len([call for call in bridge.calls if call[0] == "start"]) == 1
        await session.close()

    asyncio.run(scenario())
