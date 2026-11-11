"""Offline tests for unit replay test_caption_budget."""


import asyncio
from tests.support.replay.session import playback
from tests.support.captions.session import setup, toggle


def test_disable_reenable_and_disconnect_never_reset_aggregate_wall_budget():
    async def check():
        session, worker, clock, _ = await setup()
        try:
            session.receive("owner", playback(session, position=5))
            toggle(session)
            deadline = session.caption_state()["deadline"]
            clock.value += 5
            toggle(session, False)
            toggle(session, True)
            assert session.caption_state()["deadline"] == deadline
            session.disconnect("owner")
            assert session.caption_state()["enabled"] and session.caption_state()["user_enabled"]
            assert worker.calls[-1] == ("stop", "viewer_disconnected")
            session.connect("replacement")
            toggle(session, True, owner="replacement")
            assert session.caption_state()["deadline"] == deadline
            clock.value = deadline
            session._update_captions()
            assert not session.caption_state()["enabled"]
            assert session.caption_state()["reason"] == "budget_deadline"
            assert toggle(session, True, owner="replacement")["code"] == "captions_budget_exhausted"
        finally:
            await session.close()
    asyncio.run(check())


def test_default_on_waits_through_loading_and_pause_before_arming_immutable_deadline():
    async def check():
        session, worker, clock, _ = await setup()
        try:
            assert session.caption_state()["enabled"] and session.caption_state()["deadline"] is None
            session.receive("owner", playback(session, position=0, playing=False))
            clock.value += 45
            session.receive("owner", playback(session, position=0, stalled=True))
            assert session.caption_state()["deadline"] is None and not worker.updates
            clock.value += 45
            session.receive("owner", playback(session, position=0, playing=True))
            deadline = session.caption_state()["deadline"]
            assert deadline == clock.value + 30 and worker.updates[-1]["deadline"] == deadline
            toggle(session, False)
            clock.value += 1
            toggle(session, True)
            assert session.caption_state()["deadline"] == deadline
        finally:
            await session.close()
    asyncio.run(check())


def test_on_after_off_and_expired_budget_acknowledges_intent_with_honest_stopped_state():
    async def check():
        session, worker, clock, published = await setup()
        try:
            session.receive("owner", playback(session, position=5))
            deadline = session.caption_state()["deadline"]
            toggle(session, False)
            count = len(worker.updates)
            clock.value = deadline + 1
            assert toggle(session, True)["code"] == "captions_budget_exhausted"
            state = session.caption_state()
            assert state["user_enabled"] is True and state["enabled"] is False
            assert state["status"] == "stopped" and state["reason"] == "budget_deadline"
            assert state["deadline"] == deadline and len(worker.updates) == count
            assert published[-1]["captions"] == state
        finally:
            await session.close()
    asyncio.run(check())
