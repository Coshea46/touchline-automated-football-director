"""Offline tests for unit replay test_warm_player_handoff."""


import asyncio
import pytest
from touchline.replay import ReplaySession
from touchline.replay.config import SessionConfig
from tests.support.replay.session import Clock, playback, registry


@pytest.mark.parametrize('movement', [-0.05, -0.15, 0, 0.1])
def test_ready_source_handoff_reports_actual_time_without_a_seek_epoch(movement):
    async def scenario():
        clock = Clock()
        session = await ReplaySession(registry(), config=SessionConfig(), now=clock,
                                      monotonic=clock).initialize()
        try:
            session.connect('viewer')
            session.receive('viewer', playback(session, match_id='cam-1', position=10))
            session.receive('viewer', {'type': 'select', 'session_id': session.session_id,
                                       'match_id': 'cam-2'})
            hold = session.director.manual_hold_until
            clock.value += .1
            assert session.receive('viewer', playback(session, match_id='cam-2',
                position=10 + movement, available=True, stalled=False)) is None
            assert session.position == 10 + movement  # Never clamp or invent media time.
            assert session.clock_epoch == 0
            assert session._caption_actual_match_id == 'cam-2'
            assert session.director.current_match_id == 'cam-2'
            assert session.director.manual_hold_until == hold
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('match_id,movement,stalled', [
    ('cam-1', -.05, False),  # A seek within the same source keeps strict detection.
    ('cam-2', -.151, False),  # A misaligned destination must be realigned.
    ('cam-2', -5, False),
    ('cam-2', -.05, True),  # Only an actually available, ready handoff gets tolerance.
])
def test_seeks_and_unready_handoffs_still_reset_the_epoch(match_id, movement, stalled):
    async def scenario():
        clock = Clock()
        session = await ReplaySession(registry(), config=SessionConfig(), now=clock,
                                      monotonic=clock).initialize()
        try:
            session.connect('viewer')
            session.receive('viewer', playback(session, match_id='cam-1', position=10))
            clock.value += .1
            session.receive('viewer', playback(session, match_id=match_id,
                            position=10 + movement, available=True, stalled=stalled))
            assert session.position == 10 + movement
            assert session.clock_epoch == 1
        finally:
            await session.close()

    asyncio.run(scenario())
