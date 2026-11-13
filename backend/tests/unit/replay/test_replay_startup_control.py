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


def test_ready_workers_stay_held_during_long_tutorial_then_release_on_real_playback():
    async def scenario():
        clock = Clock()
        session = await ReplaySession(registry(), config=configuration(), bridge=FakeBridge(),
            now=clock, monotonic=clock, session_id='session').initialize()
        try:
            session.connect('viewer')
            session.receive('viewer', playback(session, playing=False))
            session._setup_until = clock() + 120
            ready = scores()
            ready.update(state='ready', scores=[])
            session.accept_snapshot(ready)
            assert session.auto_available
            for elapsed in (10, 40, 70):
                clock.value = 1000 + elapsed
                session.receive('viewer', playback(session, playing=False))
                packet = session.control()
                assert packet['startup_waiting'] is True
                assert packet['active'] is True
                assert packet['position'] == 0 and packet['playing'] is False
                assert packet['expires_at'] <= clock() + session.config.heartbeat_seconds
            session.receive('viewer', playback(session, position=.25, playing=True))
            assert session.control()['startup_waiting'] is False
            clock.value += 1
            session.receive('viewer', playback(session, position=.25, playing=False))
            assert session.control()['startup_waiting'] is False
        finally:
            await session.close()
    asyncio.run(scenario())


def test_tutorial_worker_hold_expires_and_cannot_keep_a_disconnected_viewer_active():
    async def scenario():
        clock = Clock()
        session = await ReplaySession(registry(), config=configuration(), bridge=FakeBridge(),
            now=clock, monotonic=clock, session_id='session').initialize()
        try:
            session.connect('viewer')
            session.receive('viewer', playback(session, playing=False))
            session._setup_until = clock() + 120
            ready = scores()
            ready.update(state='ready', scores=[])
            session.accept_snapshot(ready)
            assert session.control()['startup_waiting'] is True
            clock.value += session.config.heartbeat_seconds + 1
            assert session.control()['active'] is False
            assert session.control()['expires_at'] <= clock()
            clock.value = 1120
            session.receive('viewer', playback(session, playing=False))
            assert session.control()['startup_waiting'] is False
        finally:
            await session.close()
    asyncio.run(scenario())
