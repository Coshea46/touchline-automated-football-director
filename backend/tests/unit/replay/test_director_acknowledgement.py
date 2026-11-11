"""Offline tests for unit replay test_director_acknowledgement."""


import asyncio
import math
import pytest
from touchline.replay import ReplaySession
from tests.support.replay.session import Clock, FakeBridge, configuration, playback, registry, scores
from tests.support.replay.director import stable, evaluate, session_with_proposal


def test_session_score_poll_rechecks_cooldown_without_a_new_inference_snapshot():
    async def scenario():
        clock, bridge, emitted = Clock(), FakeBridge(), []
        session = await ReplaySession(registry(), config=configuration(director_mode="balanced"), bridge=bridge,
                                       publish=emitted.append, now=clock, monotonic=clock,
                                       session_id="session").initialize()
        # An already-active model must first acknowledge readiness. A manually
        # assigned running label alone cannot enable the production Director.
        ready = {**scores(sequence=0), "state": "ready", "scores": []}
        assert not session.accept_snapshot(ready)
        assert session.snapshot()["auto_available"]
        session.director.commit_selection("cam-2", now=1000, kind="automatic")
        for index, end in enumerate((5, 7, 9, 11, 13, 15)):
            evaluate(session.director, end, 1001 + index * 2, {"cam-1": 0.9, "cam-2": 0.1})
        session.position, clock.value = 15, 1012
        session._handle = "already-active"
        task = asyncio.create_task(session._score_loop())
        try:
            await asyncio.sleep(0.03)
            assert emitted[-1]["selected_match_id"] == "cam-1"
            assert emitted[-1]["observation_end"] == 15
            assert session.director.challenger_wins == 2
            assert all(call[0] != "start" for call in bridge.calls)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await session.close()

    asyncio.run(scenario())


def test_automatic_cooldown_starts_at_ack_and_duplicate_ack_does_not_renew_history():
    async def scenario():
        session, clock, bridge, proposal = await session_with_proposal()
        assert proposal["selected_match_id"] == "cam-2"
        assert not math.isfinite(session.director.last_automatic_switch_at)
        clock.value += 0.3
        ack = {"type": "ack", "session_id": "session", "clock_epoch": 0,
               "sequence": proposal["sequence"], "match_id": "cam-2"}
        assert session.receive("owner", ack) is None
        committed_at = session.director.last_automatic_switch_at
        assert committed_at == clock.value and len(session.director.history) == 1
        assert session.director.history[-1]["kind"] == "automatic"
        assert session.director.history[-1]["score"] == 0.8
        clock.value += 1
        assert session.receive("owner", ack) is None
        assert session.director.last_automatic_switch_at == committed_at
        assert len(session.director.history) == 1 and not bridge.calls
        await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("invalidate", ["expiry", "mode", "manual"])
def test_expired_or_superseded_proposals_cannot_commit_automatic_selection(invalidate):
    async def scenario():
        session, clock, _, proposal = await session_with_proposal()
        if invalidate == "expiry":
            clock.value += 4
        elif invalidate == "mode":
            session.receive("owner", {"type": "director_mode", "session_id": "session", "mode": "relaxed"})
        else:
            session.receive("owner", {"type": "select", "session_id": "session", "match_id": "cam-1"})
        error = session.receive("owner", {"type": "ack", "session_id": "session", "clock_epoch": 0,
                                           "sequence": proposal["sequence"], "match_id": "cam-2"})
        assert error["code"] == "stale_ack"
        assert session.director.current_match_id == "cam-1"
        assert not math.isfinite(session.director.last_automatic_switch_at)
        assert not session.director.history
        await session.close()

    asyncio.run(scenario())


def test_browser_playability_cannot_revive_an_unavailable_model_worker():
    async def scenario():
        session = await ReplaySession(registry(), config=configuration(), bridge=FakeBridge(),
                                       session_id="session").initialize()
        session.connect("owner")
        session.receive("owner", playback(session, position=5))
        snapshot = scores({"cam-2": 0.8}, now=session.now())
        snapshot["health"]["cam-1"] = "error"
        assert session.accept_snapshot(snapshot)
        assert not session.director.healthy["cam-1"]
        session.receive("owner", playback(session, match_id="cam-1", position=5, available=True))
        assert not session.director.healthy["cam-1"]
        await session.close()

    asyncio.run(scenario())


def test_expired_proposal_cannot_be_classified_as_automatic_by_actual_playback_feedback():
    async def scenario():
        session, clock, _, _ = await session_with_proposal()
        clock.value += 4
        session.receive("owner", playback(session, match_id="cam-2", position=7))
        assert session.director.current_match_id == "cam-2"  # The reported source is still factual.
        assert session.director.history[-1]["kind"] == "playback"
        assert not math.isfinite(session.director.last_automatic_switch_at)
        assert session.director.state(now=clock.value, playback_position=7)["auto_cooldown_until"] == clock.value + 12
        await session.close()

    asyncio.run(scenario())


def test_health_loss_invalidates_pending_ack_even_if_target_later_recovers():
    async def scenario():
        session, clock, _, proposal = await session_with_proposal()
        health = scores(end=7, sequence=3, now=clock.value)
        health.update(scores=[], health={"cam-1": "healthy", "cam-2": "error"})
        assert not session.accept_snapshot(health)
        assert not session.director.healthy["cam-2"]
        assert session._last_recommendation is None and session.director.challenger_wins == 0
        ack = {"type": "ack", "session_id": "session", "clock_epoch": 0,
               "sequence": proposal["sequence"], "match_id": "cam-2"}
        assert session.receive("owner", ack)["code"] == "stale_ack"
        assert session.director.current_match_id == "cam-1" and not session.director.history
        assert not math.isfinite(session.director.last_automatic_switch_at)
        recovered = scores(end=7, sequence=4, now=clock.value)
        recovered["scores"] = []
        session.accept_snapshot(recovered)
        assert session.director.healthy["cam-2"]
        assert session.receive("owner", ack)["code"] == "stale_ack"
        assert session.director.current_match_id == "cam-1" and not session.director.history
        await session.close()

    asyncio.run(scenario())


def test_health_loss_cannot_make_playback_feedback_an_automatic_commit():
    async def scenario():
        session, clock, _, _ = await session_with_proposal()
        health = scores(end=7, sequence=3, now=clock.value)
        health.update(scores=[], health={"cam-1": "healthy", "cam-2": "error"})
        session.accept_snapshot(health)
        session.receive("owner", playback(session, match_id="cam-2", position=7, available=True))
        assert session.director.current_match_id == "cam-2"
        assert not session.director.healthy["cam-2"]
        assert session.director.history[-1]["kind"] == "playback"
        assert not math.isfinite(session.director.last_automatic_switch_at)
        await session.close()

    asyncio.run(scenario())


def test_unready_target_playback_does_not_start_automatic_cooldown():
    async def scenario():
        session, _, _, _ = await session_with_proposal()
        session.receive("owner", playback(session, match_id="cam-2", position=7,
                                           available=False, stalled=True))
        assert session.director.current_match_id == "cam-1"
        assert not session.director.history
        assert not math.isfinite(session.director.last_automatic_switch_at)
        await session.close()

    asyncio.run(scenario())


def test_loading_target_commits_automatic_only_when_frame_becomes_ready():
    async def scenario():
        session, clock, _, proposal = await session_with_proposal()
        session.receive("owner", playback(session, match_id="cam-2", position=7,
                                           available=True, stalled=True))
        assert session.director.current_match_id == "cam-1" and not session.director.history
        assert not math.isfinite(session.director.last_automatic_switch_at)
        clock.value += 0.2
        session.receive("owner", playback(session, match_id="cam-2", position=7,
                                           available=True, stalled=False))
        assert session.director.current_match_id == "cam-2"
        assert session.director.history[-1]["kind"] == "automatic"
        assert session.director.last_automatic_switch_at == clock.value
        committed = session.director.last_automatic_switch_at
        assert session.receive("owner", {"type": "ack", "session_id": "session", "clock_epoch": 0,
                                          "sequence": proposal["sequence"], "match_id": "cam-2"}) is None
        assert len(session.director.history) == 1 and session.director.last_automatic_switch_at == committed
        await session.close()

    asyncio.run(scenario())


def test_unattributed_ready_change_still_guards_next_auto_switch_and_manual_hold_wins_fallback():
    d = stable()
    d.commit_selection("cam-2", now=1000, kind="playback", reason="playback_feedback")
    assert not math.isfinite(d.last_automatic_switch_at)
    evaluate(d, 5, 1002, {"cam-1": 0.9, "cam-2": 0.1})
    guarded = evaluate(d, 7, 1004, {"cam-1": 0.9, "cam-2": 0.1})
    assert guarded["selected_match_id"] == "cam-2" and guarded["selection_reason"] == "automatic_cooldown"
    d.manual_select("cam-2", now=1005)
    d.healthy["cam-2"] = False  # Its video can still be watched while monitoring is unavailable.
    assert evaluate(d, 9, 1006, {"cam-1": 0.9}) is None
    assert d.current_match_id == "cam-2" and d.decision_reason == "manual_hold"
    assert evaluate(d, 11, 1036, {"cam-1": 0.9})["selection_kind"] == "fallback"


@pytest.mark.parametrize("terminal", ["error", "stopped"])
def test_terminal_snapshot_revokes_cached_proposal_and_disables_timer_and_ack(terminal):
    async def scenario():
        session, clock, _, proposal = await session_with_proposal()
        ending = scores(end=7, sequence=99, now=clock.value)
        ending.update(state=terminal, scores=[], health={},
                      reason="checkpoint_unavailable" if terminal == "error" else "session_limit")
        assert not session.accept_snapshot(ending)
        assert session._last_recommendation is None and session.director.challenger_wins == 0
        assert not session._evaluate_director()
        assert session.receive("owner", {"type": "ack", "session_id": "session", "clock_epoch": 0,
                                          "sequence": proposal["sequence"], "match_id": "cam-2"})["code"] == "stale_ack"
        assert session.director.current_match_id == "cam-1" and not session.director.history
        late = scores(end=9, sequence=100, now=clock.value)
        assert not session.accept_snapshot(late)
        assert session.state in ("error", "stopped")
        await session.close()

    asyncio.run(scenario())
