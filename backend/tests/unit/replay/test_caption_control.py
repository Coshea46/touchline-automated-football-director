"""Offline tests for unit replay test_caption_control."""


import asyncio
import json
import threading
from tests.support.replay.session import configuration, playback, scores
from tests.support.captions.session import CaptionWorker, setup, toggle, event


def test_controller_only_caption_toggle_with_automatic_default_and_no_work_before_playback():
    async def check():
        session, worker, _, _ = await setup()
        try:
            session.connect("observer")
            assert session.caption_state()["user_enabled"] and session.caption_state()["enabled"]
            assert not worker.updates
            assert toggle(session, owner="observer")["code"] == "viewer_in_use"
            assert toggle(session, "true")["code"] == "invalid_captions"
            assert session.receive("owner", {"type": "captions", "session_id": "old", "enabled": True})["code"] == "session_mismatch"
            session.receive("owner", playback(session, position=5))
            assert len(worker.updates) == 1
            state = session.snapshot()["captions"]
            assert state["available"] and state["enabled"] and state["configured"] and state["user_enabled"]
            assert "authorized" not in state
            assert state["model"] == "scribe_v2_realtime" and state["max_audio_seconds"] == 30
        finally:
            await session.close()
    asyncio.run(check())


def test_automatic_proposal_stops_audio_without_delaying_or_restarting_inference():
    async def check():
        session, worker, _, published = await setup(config=configuration())
        try:
            session.receive("owner", playback(session, position=5))
            toggle(session)
            old = event(session)
            session.state = "running"
            session.accept_snapshot(scores())
            recommendation = next(message for message in published if message["type"] == "recommendation")
            assert recommendation["selected_match_id"] == "cam-2"
            assert worker.calls[-1] == ("stop", "automatic_selection")
            assert not session._accept_caption_event(old)
            assert not session.bridge.calls and not session._stop_requested
        finally:
            await session.close()
    asyncio.run(check())


def test_transport_update_failure_stops_effective_state_without_autonomous_retry():
    class FailedWorker(CaptionWorker):
        def update(self, view):
            super().update(view)
            raise RuntimeError("PRIVATE_DETAILS")

    async def check():
        session, worker, _, _ = await setup(worker=FailedWorker())
        try:
            session.receive("owner", playback(session, position=5))
            count = len(worker.updates)
            for _ in range(3):
                session._update_captions()
            state = session.caption_state()
            assert state["user_enabled"] and not state["enabled"]
            assert state["reason"] == "transport_error" and len(worker.updates) == count
            assert "PRIVATE" not in json.dumps(state)
        finally:
            await session.close()
    asyncio.run(check())


def test_partial_and_committed_events_are_compact_and_keep_media_lag_distinct():
    async def check():
        session, _, _, published = await setup()
        try:
            session.receive("owner", playback(session, position=5))
            toggle(session)
            assert session._accept_caption_event(event(session, unknown_secret="DO-NOT-EXPOSE"))
            assert session._accept_caption_event(event(session, state="committed", latency_seconds=0.2,
                words=[{"text": "fixture", "start_seconds": 3, "end_seconds": 4}]))
            messages = [message for message in published if message["type"] == "caption"]
            assert [message["state"] for message in messages] == ["partial", "committed"]
            assert "words" not in messages[0] and "latency_seconds" not in messages[0]
            assert messages[1]["playback_lag_seconds"] == 1 and messages[1]["latency_seconds"] == 0.2
            assert messages[1]["received_at"] == 1000 and messages[1]["sequence"] > messages[0]["sequence"]
            assert "DO-NOT-EXPOSE" not in json.dumps(messages)
        finally:
            await session.close()
    asyncio.run(check())


def test_provider_terminal_event_disables_retry_and_drops_old_callbacks():
    async def check():
        session, worker, _, _ = await setup()
        try:
            session.receive("owner", playback(session, position=5))
            toggle(session)
            previous = event(session)
            assert session._accept_caption_event(event(session, state="error", reason="quota_exhausted", text=""))
            count = len(worker.updates)
            session._update_captions()
            assert len(worker.updates) == count and not session.caption_state()["enabled"]
            assert session.caption_state()["user_enabled"] is True
            assert not session._accept_caption_event(previous)
        finally:
            await session.close()
    asyncio.run(check())


def test_worker_callbacks_are_threadsafe_and_close_is_off_loop():
    async def check():
        session, worker, _, published = await setup()
        session.start()
        try:
            session.receive("owner", playback(session, position=5))
            toggle(session)
            await asyncio.to_thread(session.caption_event, event(session))
            await asyncio.sleep(0)
            assert any(message["type"] == "caption" for message in published)
        finally:
            await session.close()
        assert worker.calls[-1][0] == "close" and worker.calls[-1][1] != threading.get_ident()
        assert session.metrics["caption_stop_confirmed"]
    asyncio.run(check())


def test_forced_or_unconfirmed_child_stop_is_not_reported_as_confirmed():
    class UnconfirmedWorker(CaptionWorker):
        def close(self):
            return False

    async def check():
        session, _, _, _ = await setup(worker=UnconfirmedWorker())
        await session.close()
        assert session.metrics["caption_stop_confirmed"] is False
    asyncio.run(check())
