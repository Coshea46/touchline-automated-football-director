import asyncio
from touchline.hosted.sessions import SessionManager
from tests.support.hosted import Admission, Connection, Factory
from tests.support.replay.session import playback


def test_setup_requires_factual_zero_paused_report_and_charges_only_once():
    async def scenario():
        factory, admission, connection = Factory(), Admission(), Connection()
        manager = SessionManager(factory, admission, now=factory.clock)
        try:
            visitor, _ = await manager.attach({'type': 'hello'}, 'peer', connection)
            setup = {'type': 'setup', 'session_id': visitor.session.session_id}
            assert (await manager.receive(visitor, connection, setup))['code'] == 'invalid_playback'
            assert not admission.acquired
            report = playback(visitor.session, position=0)
            report.update(playing=False, stalled=False, available=True)
            assert await manager.receive(visitor, connection, report) is None
            assert not admission.acquired
            assert await manager.receive(visitor, connection, setup) is None
            assert await manager.receive(visitor, connection, setup) is None
            assert len(admission.acquired) == 1
            assert visitor.session.position == 0 and not visitor.session.playing
            assert visitor.session._setup_until == factory.clock() + 120
            await visitor.session._control_tick()
            assert factory.bridges[0].calls
            assert visitor.session.position == 0 and not visitor.session.playing
            manager.detach(visitor, connection)
            await visitor.stop_task
            assert admission.released == [('permit-0', True)]
        finally:
            await manager.close()
    asyncio.run(scenario())
