"""Offline tests for unit replay test_replay_startup_control."""


import asyncio
from touchline.replay import ReplaySession
from tests.support.replay.session import Clock, FakeBridge, configuration, playback, registry, scores


def test_backend_startup_barrier_tracks_actual_group_readiness_and_stop():
    async def scenario():
        clock, bridge = Clock(), FakeBridge()
        session = await ReplaySession(registry(), config=configuration(), bridge=bridge,
                                       now=clock, monotonic=clock, session_id='session').initialize()
        session.connect('viewer')
        session.receive('viewer', playback(session, playing=False))
        before = session.control()
        assert before['startup_waiting'] is True and before['active'] is True
        ready = scores()
        ready.update(state='ready', scores=[])
        session.accept_snapshot({**ready, 'model_version': 'wrong-model'})
        assert session.control()['startup_waiting'] is True
        session.accept_snapshot(ready)
        released = session.control()
        assert released['startup_waiting'] is False
        assert released['position'] == before['position'] == 0
        assert released['expires_at'] == before['expires_at']
        session._handle = 'owned-fake-handle'
        await session._stop_worker()
        assert session.control()['startup_waiting'] is True
        await session.close()

    asyncio.run(scenario())
