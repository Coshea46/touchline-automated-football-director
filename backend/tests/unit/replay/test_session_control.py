"""Offline tests for unit replay test_session_control."""


import asyncio
import json
from types import SimpleNamespace
import pytest
from touchline.replay import ReplaySession
from touchline.replay.config import SessionConfig
from touchline.api.websocket import Connection, WebSocketManager
from tests.support.replay.session import Clock, FakeBridge, registry, configuration, scores, playback


def test_auto_default_activates_only_after_identity_valid_worker_readiness_and_stops_on_failure():
    async def scenario():
        bridge = FakeBridge()
        session = await ReplaySession(registry(), config=configuration(), bridge=bridge,
                                       session_id="session").initialize()
        initial = session.connect("viewer")
        assert initial["auto_requested"] is True
        assert initial["auto_enabled"] is False and initial["auto_available"] is False
        enable = {"type": "auto", "session_id": "session", "enabled": True}
        assert session.receive("viewer", enable)["code"] == "auto_unavailable"
        ready = scores()
        ready.update(state="ready", scores=[])
        assert not session.accept_snapshot({**ready, "model_version": "wrong-model"})
        assert not session.snapshot()["auto_available"]
        assert not session.accept_snapshot(ready)  # Readiness is valid without a fabricated score.
        active = session.snapshot()
        assert active["auto_available"] is True and active["auto_enabled"] is True
        assert active["auto_requested"] is True
        failure = {**scores(sequence=2), "state": "error", "reason": "checkpoint_unavailable"}
        assert not session.accept_snapshot(failure)
        failed = session.snapshot()
        assert failed["auto_available"] is False and failed["auto_enabled"] is False
        assert failed["auto_requested"] is True
        assert session.receive("viewer", enable)["code"] == "auto_unavailable"
        assert session.receive("viewer", {**enable, "enabled": False}) is None
        assert session.snapshot()["auto_requested"] is False
        assert not bridge.calls  # Readiness controls do not launch an SDK or GPU job.
        await session.close()

    asyncio.run(scenario())


def test_explicit_auto_off_during_startup_survives_readiness_then_on_resumes():
    async def scenario():
        session = await ReplaySession(registry(), config=configuration(), bridge=FakeBridge(),
                                       session_id="session").initialize()
        session.connect("viewer")
        toggle = {"type": "auto", "session_id": "session", "enabled": False}
        assert session.receive("viewer", toggle) is None
        assert session.snapshot()["auto_requested"] is False
        ready = scores()
        ready.update(state="ready", scores=[])
        session.accept_snapshot(ready)
        active = session.snapshot()
        assert active["auto_available"] is True
        assert active["auto_enabled"] is False and active["auto_requested"] is False
        assert session.receive("viewer", {**toggle, "enabled": True}) is None
        assert session.snapshot()["auto_enabled"] is True
        assert session.snapshot()["auto_requested"] is True
        await session.close()

    asyncio.run(scenario())


def test_disabled_session_accepts_auto_off_but_never_auto_on():
    async def scenario():
        session = await ReplaySession(registry(), config=SessionConfig()).initialize()
        session.connect("viewer")
        toggle = {"type": "auto", "session_id": session.session_id, "enabled": False}
        assert not session.snapshot()["auto_available"]
        assert session.receive("viewer", toggle) is None
        assert session.receive("viewer", {**toggle, "enabled": True})["code"] == "auto_unavailable"
        assert not session.snapshot()["auto_enabled"]
        await session.close()

    asyncio.run(scenario())


def test_transport_defaults_to_private_websocket_without_importing_it_when_disabled(monkeypatch):
    import sys

    calls = []

    class WebSocketClient:
        @classmethod
        def from_env(cls):
            calls.append("private_ws")
            return FakeBridge()

    monkeypatch.setitem(sys.modules, "touchline.ml_inference.streaming.ws_client",
                        SimpleNamespace(ReplayWebSocketClient=WebSocketClient))

    async def scenario():
        disabled = await ReplaySession(registry(), config=SessionConfig.from_env({})).initialize()
        assert disabled.config.replay_transport == "websocket" and not calls
        await disabled.close()
        config = SessionConfig.from_env({"MATCHPULSE_MONITOR": "true", "MATCHPULSE_ML_MODE": "features_mock",
                                         "MATCHPULSE_MODAL_WS_URL": "https://private.modal.run/session",
                                         "MATCHPULSE_MODAL_WS_TOKEN": "private-super-secret-token"})
        session = await ReplaySession(registry(), config=config).initialize()
        session.connect("viewer")
        assert calls == ["private_ws"]
        public = json.dumps({"session": session.snapshot(), "metrics": session.metrics})
        assert "private.modal.run" not in public and "private-super-secret-token" not in public
        assert session.metrics["replay_transport"] == "websocket"
        await session.close()

    asyncio.run(scenario())
    assert SessionConfig.from_env({"MATCHPULSE_REPLAY_TRANSPORT": "dict"}).replay_transport == "dict"
    with pytest.raises(ValueError, match="REPLAY_TRANSPORT"):
        SessionConfig.from_env({"MATCHPULSE_REPLAY_TRANSPORT": "unknown"})


@pytest.mark.parametrize("result,confirmed", [(None, False), ({"confirmed": False}, False),
                                             ({"confirmed": True, "method": "websocket_graceful"}, True)])
def test_websocket_stop_confirmation_distinguishes_acknowledgement_from_socket_close(result, confirmed):
    async def scenario():
        class SocketBridge(FakeBridge):
            transport = "websocket"

            def stop(self, session_id, handle):
                return result

        session = await ReplaySession(registry(), config=configuration(), bridge=SocketBridge()).initialize()
        session._handle = "owned-socket-session"
        await session.close()
        assert session.metrics["worker_stop_succeeded"] is confirmed
        assert session.metrics["owned_stop_method"] == "websocket_graceful"
        assert session.metrics["shutdown_cancel_status"] == ("completed" if confirmed else "unconfirmed")

    asyncio.run(scenario())


def test_failed_playback_publishes_health_and_allows_valid_scored_fallback_during_hold():
    async def scenario():
        clock = Clock()
        sent = []
        session = await ReplaySession(registry(), config=configuration(), bridge=FakeBridge(),
                                       publish=sent.append, now=clock, monotonic=clock,
                                       session_id="session").initialize()
        session.connect("viewer")
        session.director.manual_select("cam-1", now=clock.value)
        session.director.auto_enabled = False
        session.receive("viewer", playback(session, position=5, available=False))
        assert sent[-1]["type"] == "session"
        assert not sent[-1]["matches"][0]["healthy"]
        assert not session.accept_snapshot(scores())
        assert session.director.current_match_id == "cam-1"
        assert sent[-1]["director"]["latest_scores"] == {"cam-2": 0.8}
        session.director.manual_hold_until = 0  # Fallback is evaluated after the user's hold.
        session.receive("viewer", {"type": "auto", "session_id": "session", "enabled": True})
        assert session._evaluate_director()
        assert sent[-1]["selected_match_id"] == "cam-2"
        assert "cam-1" not in sent[-1]["scores"]
        await session.close()

    asyncio.run(scenario())


def test_session_clock_manual_selection_roles_and_no_extrapolation():
    async def scenario():
        clock = Clock()
        bridge = FakeBridge()
        session = await ReplaySession(registry(), config=configuration(), bridge=bridge,
                                       now=clock, monotonic=clock, session_id="session").initialize()
        assert session.connect("first")["controls_enabled"]
        assert not session.connect("second")["controls_enabled"]
        assert session.receive("second", playback(session))["code"] == "viewer_in_use"
        assert session.receive("first", playback(session, position=10)) is None
        clock.value += 2
        assert session.control()["position"] == 10
        assert session.control()["expires_at"] == clock.value + 3
        assert not bridge.calls  # A frontend report doesn't make an SDK call.
        assert session.receive("first", {"type": "select", "session_id": "session",
                                          "match_id": "cam-2"}) is None
        assert session.director.current_match_id == "cam-2"
        assert session.director.manual_hold_until == clock.value + 30
        assert session.receive("first", playback(session, position=1)) is None
        assert session.clock_epoch == 1
        session.disconnect("first")
        assert session.controls_enabled("second")
        assert not session.control()["active"]
        await session.close()

    asyncio.run(scenario())


def test_heartbeat_expiration_pause_idle_eof_and_duration_cleanup():
    async def scenario():
        clock = Clock()
        bridge = FakeBridge()
        session = await ReplaySession(registry(), config=configuration(max_duration_seconds=10),
                                       bridge=bridge, now=clock, monotonic=clock).initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session))
        await session._control_tick()
        clock.value += 6
        await session._control_tick()
        assert session._handle is None and session.state == "waiting"
        assert any(call[0] == "stop" for call in bridge.calls)
        clock.value += 5
        session.receive("viewer", playback(session, ended=True))
        await session._control_tick()
        assert session.state == "stopped" and session._finished
        await session.close()

    asyncio.run(scenario())


def test_pending_socket_messages_keep_only_latest_per_type():
    async def scenario():
        session = await ReplaySession(registry()).initialize()
        manager = WebSocketManager(session)
        connection = Connection(None)
        for sequence in range(1000):
            manager._queue("one", connection, {"type": "recommendation", "sequence": sequence})
        assert len(connection.pending) == 1
        assert connection.pending["recommendation"]["sequence"] == 999
        await session.close()

    asyncio.run(scenario())


def test_discarded_proposal_keeps_actual_viewer_clock_and_late_ack_cannot_undo_manual_pick():
    async def scenario():
        clock = Clock()
        sent = []
        session = await ReplaySession(registry(), config=configuration(), bridge=FakeBridge(),
                                       publish=sent.append, now=clock, monotonic=clock,
                                       session_id="session").initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session, position=5))
        assert session.accept_snapshot(scores())
        proposal = sent[-1]
        assert proposal["selected_match_id"] == "cam-2"
        assert session.receive("viewer", playback(session, match_id="cam-1", position=5)) is None
        assert session.director.current_match_id == "cam-1"
        session.receive("viewer", {"type": "select", "session_id": "session", "match_id": "cam-1"})
        error = session.receive("viewer", {"type": "ack", "session_id": "session",
                                            "sequence": proposal["sequence"], "clock_epoch": 0,
                                            "match_id": "cam-2"})
        assert error["code"] == "stale_ack"
        assert session.director.current_match_id == "cam-1"
        await session.close()

    asyncio.run(scenario())


def test_startup_hold_does_not_trigger_idle_gpu_shutdown_and_pause_later_does():
    async def scenario():
        clock = Clock()
        bridge = FakeBridge()
        session = await ReplaySession(registry(), config=configuration(), bridge=bridge,
                                       now=clock, monotonic=clock, session_id="session").initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session, playing=False))
        await session._control_tick()
        clock.value += 20
        session.receive("viewer", playback(session, playing=False))
        await session._control_tick()
        assert session._handle == "own-handle" and session.state == "starting"
        ready = scores(now=clock.value)
        ready.update(state="ready", scores=[])
        session.accept_snapshot(ready)
        clock.value += 11
        session.receive("viewer", playback(session, playing=False))
        await session._control_tick()
        assert session._handle is None and session.state == "paused"
        # Fresh pause heartbeats don't create another GPU input.
        await session._control_tick()
        assert len([call for call in bridge.calls if call[0] == "start"]) == 1
        session.receive("viewer", playback(session, playing=True))
        await session._control_tick()
        assert len([call for call in bridge.calls if call[0] == "start"]) == 2
        await session.close()

    asyncio.run(scenario())
