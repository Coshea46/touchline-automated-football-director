"""Offline tests for unit replay test_caption_clock."""


import asyncio
import pytest
from tests.support.replay.session import playback
from tests.support.captions.session import setup, toggle, event


def test_actual_report_permit_lease_never_extrapolates_or_renews_on_background_ticks():
    async def check():
        session, worker, clock, _ = await setup()
        try:
            session.receive("owner", playback(session, position=5, rate=2))
            toggle(session)
            initial = worker.updates[-1]
            assert initial["position"] == 5 and initial["expires_at"] == 1005
            clock.value += 4
            session._update_captions()
            repeated = worker.updates[-1]
            assert repeated["position"] == 5 and repeated["expires_at"] == initial["expires_at"]
            assert repeated["report_received_at"] == 1000
            clock.value += 1
            session._update_captions()
            assert session.caption_state()["status"] == "paused"
            assert worker.calls[-1] == ("stop", "playback_inactive")
        finally:
            await session.close()
    asyncio.run(check())


@pytest.mark.parametrize("change", [{"playing": False}, {"stalled": True}, {"available": False}, {"ended": True}])
def test_pause_buffer_unavailability_and_eof_cancel_current_generation_immediately(change):
    async def check():
        session, worker, _, _ = await setup()
        try:
            session.receive("owner", playback(session, position=5))
            toggle(session)
            old = event(session)
            generation = session._caption_generation
            session.receive("owner", playback(session, position=5, **change))
            assert session._caption_generation > generation
            assert worker.calls[-1][0] == "stop"
            assert not session._accept_caption_event(old)
        finally:
            await session.close()
    asyncio.run(check())


def test_manual_switch_stops_old_audio_until_actual_ready_target_report():
    async def check():
        session, worker, _, _ = await setup()
        try:
            session.receive("owner", playback(session, position=5))
            toggle(session)
            count = len(worker.updates)
            assert session.receive("owner", {"type": "select", "session_id": "session", "match_id": "cam-2"}) is None
            assert worker.calls[-1] == ("stop", "manual_selection")
            session.receive("owner", playback(session, match_id="cam-1", position=5.1))
            assert len(worker.updates) == count
            session.receive("owner", playback(session, match_id="cam-2", position=5.1, stalled=True))
            assert len(worker.updates) == count
            session.receive("owner", playback(session, match_id="cam-2", position=5.2))
            assert worker.updates[-1]["match_id"] == "cam-2"
            assert worker.updates[-1]["source_key"] == "match-2.mp4"
        finally:
            await session.close()
    asyncio.run(check())


def test_preparing_same_source_requires_a_new_actual_ready_report():
    async def check():
        session, worker, clock, _ = await setup()
        try:
            session.receive("owner", playback(session, position=5))
            toggle(session)
            count = len(worker.updates)
            session.receive("owner", {"type": "source_preparing", "session_id": "session", "match_id": "cam-1"})
            session._update_captions()
            assert len(worker.updates) == count
            clock.value += .25
            session.receive("owner", playback(session, position=5))
            assert len(worker.updates) == count + 1
        finally:
            await session.close()
    asyncio.run(check())


def test_seek_invalidates_caption_epoch_and_resumes_only_at_new_permitted_position():
    async def check():
        session, worker, _, _ = await setup()
        try:
            session.receive("owner", playback(session, position=8))
            toggle(session)
            old = event(session)
            session.receive("owner", playback(session, position=2))
            assert session.clock_epoch == 1
            assert ("stop", "seek") in worker.calls
            assert worker.updates[-1]["position"] == 2 and worker.updates[-1]["clock_epoch"] == 1
            assert not session._accept_caption_event(old)
        finally:
            await session.close()
    asyncio.run(check())


def test_explicit_off_persists_through_switch_seek_and_reconnect_until_explicit_on():
    async def check():
        session, worker, clock, _ = await setup()
        try:
            session.receive("owner", playback(session, position=5))
            deadline = session.caption_state()["deadline"]
            toggle(session, False)
            count = len(worker.updates)
            session.receive("owner", {"type": "select", "session_id": "session", "match_id": "cam-2"})
            clock.value += .25
            session.receive("owner", playback(session, match_id="cam-2", position=4))
            assert session.clock_epoch == 1
            session.disconnect("owner")
            snapshot = session.connect("replacement")
            assert snapshot["captions"]["user_enabled"] is False and snapshot["captions"]["enabled"] is False
            session.receive("replacement", playback(session, match_id="cam-2", position=4.25))
            assert len(worker.updates) == count
            toggle(session, True, owner="replacement")
            resumed = worker.updates[-1]
            assert resumed["match_id"] == "cam-2" and resumed["position"] == 4.25
            assert resumed["clock_epoch"] == 1 and resumed["deadline"] == deadline
        finally:
            await session.close()
    asyncio.run(check())


@pytest.mark.parametrize("mutation", [
    {"session_id": "old"}, {"generation": 0}, {"clock_epoch": 1}, {"match_id": "cam-2"},
    {"source_key": "other.mp4"}, {"source_identity": "0" * 64}, {"model": "scribe_v2"},
    {"source_anchor_seconds": 100}, {"source_anchor_seconds": None},
    {"words": [{"text": "future", "start_seconds": 5, "end_seconds": 7}]},
    {"words": [{"text": "bad", "start_seconds": 0, "end_seconds": float("nan")}]},
])
def test_old_wrong_source_or_future_caption_text_is_never_broadcast(mutation):
    async def check():
        session, _, _, published = await setup()
        try:
            session.receive("owner", playback(session, position=5))
            toggle(session)
            assert not session._accept_caption_event(event(session, **mutation))
            assert not any(message["type"] == "caption" for message in published)
        finally:
            await session.close()
    asyncio.run(check())
