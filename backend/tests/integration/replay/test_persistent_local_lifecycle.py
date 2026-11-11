"""Offline tests for integration replay test_persistent_local_lifecycle."""


import asyncio
import json
import websockets
from touchline.main import AppController
from touchline.replay.config import SessionConfig
from touchline.replay import SourceRegistry
from tests.support.captions.session import CaptionWorker, event, setup
from tests.support.replay.session import Clock, FakeBridge, playback


def test_caption_budget_reason_and_deadline_survive_switch_seek_and_reconnect():
    async def scenario():
        session, worker, clock, _ = await setup()
        try:
            session.receive('owner', playback(session, position=5))
            deadline = session.caption_state()['deadline']
            old = event(session)
            clock.value = deadline + 1
            session.receive('owner', playback(session, position=36))
            assert session.caption_state()['reason'] == 'budget_deadline'
            generation, updates = session._caption_generation, len(worker.updates)
            session.receive('owner', {'type': 'select', 'session_id': 'session', 'match_id': 'cam-2'})
            session.receive('owner', playback(session, match_id='cam-2', position=3))
            session.disconnect('owner')
            state = session.connect('new-owner')['captions']
            assert state['status'] == 'stopped' and state['reason'] == 'budget_deadline'
            assert state['deadline'] == deadline and state['user_enabled'] and not state['enabled']
            assert session._caption_generation > generation and len(worker.updates) == updates
            assert not session._accept_caption_event(old)
            assert session.receive('new-owner', {'type': 'captions', 'session_id': 'session',
                                                 'enabled': True})['code'] == 'captions_budget_exhausted'
            assert session.caption_state()['deadline'] == deadline and len(worker.updates) == updates
        finally:
            await session.close()

    asyncio.run(scenario())


def test_persistent_websocket_and_tables_news_api_survive_bounded_inference_and_caption_expiry(monkeypatch):
    for name in ('FOOTBALL_DATA_API_KEY', 'GUARDIAN_API_KEY', 'ELEVENLABS_API_KEY'):
        monkeypatch.setenv(name, '')

    async def scenario():
        bridge, captions, clock = FakeBridge(), CaptionWorker(), Clock()
        config = SessionConfig(monitor=True, mode='features_mock', model_version='mock-v1',
                               max_duration_seconds=10, max_estimated_cost_usd=1, max_restarts=0)
        controller = AppController(registry=SourceRegistry.from_env({}), config=config, bridge=bridge,
                                   host='127.0.0.1', port=0, api_port=0, api_enabled=True, caption_worker=captions)
        session = controller.session
        session.now, session.monotonic = clock, clock
        bridge.snapshot = {'session_id': session.session_id, 'model_version': 'mock-v1', 'clock_epoch': 0,
                           'worker_sequence': 1, 'score_kind': 'mock', 'state': 'ready', 'scores': [],
                           'health': {f'cam-{i}': 'healthy' for i in range(1, 8)}}
        task = asyncio.create_task(controller.start())

        async def wait_until(predicate):
            async def poll():
                while not predicate():
                    if task.done():
                        await task
                    await asyncio.sleep(.001)
            await asyncio.wait_for(poll(), 3)

        async def get(path):
            reader, writer = await asyncio.open_connection('127.0.0.1', controller.api_manager.port)
            writer.write(f'GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'.encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 3)
            writer.close()
            await writer.wait_closed()
            header, body = response.split(b'\r\n\r\n', 1)
            assert header.startswith(b'HTTP/1.1 200')
            return json.loads(body)

        async def receive_until(socket, predicate):
            async def receive():
                while True:
                    value = json.loads(await socket.recv())
                    if predicate(value):
                        return value
            return await asyncio.wait_for(receive(), 3)

        try:
            await wait_until(lambda: controller.ws_manager.server is not None)
            port = controller.ws_manager.server.sockets[0].getsockname()[1]
            async with websockets.connect(f'ws://127.0.0.1:{port}') as socket:
                initial = json.loads(await socket.recv())
                assert len(initial['matches']) == 7
                await socket.send(json.dumps(playback(session, position=1)))
                await wait_until(lambda: session._worker_ready)
                deadline = session.caption_state()['deadline']
                clock.value += 11
                await socket.send(json.dumps(playback(session, position=12)))
                await wait_until(lambda: session.state == 'stopped' and session._handle is None)
                stopped = await receive_until(socket, lambda value: value.get('type') == 'session'
                    and value.get('error_code') == 'session_limit_reached')
                assert not stopped['auto_available'] and not task.done()
                assert sum(call[0] == 'start' for call in bridge.calls) == 1
                assert len((await get('/api/catalog'))['competitions']) >= 8
                assert (await get('/api/standings?competition=PL'))['status'] == 'missing_key'
                assert (await get('/api/news?topic=football'))['status'] == 'missing_key'
                clock.value = deadline + 1
                await socket.send(json.dumps(playback(session, position=33)))
                await wait_until(lambda: session.caption_state()['reason'] == 'budget_deadline')
                await socket.send(json.dumps({'type': 'select', 'session_id': session.session_id,
                                               'match_id': 'cam-2'}))
                selected = await receive_until(socket, lambda value: value.get('type') == 'session'
                    and value.get('current_match_id') == 'cam-2')
                assert selected['captions']['reason'] == 'budget_deadline'
                assert selected['captions']['deadline'] == deadline and not task.done()
            await wait_until(lambda: not session.clients)
            async with websockets.connect(f'ws://127.0.0.1:{port}') as reconnected:
                state = json.loads(await reconnected.recv())
                assert state['state'] == 'stopped' and state['current_match_id'] == 'cam-2'
                assert state['captions']['reason'] == 'budget_deadline'
                assert state['captions']['deadline'] == deadline
                assert not session._closed and sum(call[0] == 'start' for call in bridge.calls) == 1
                assert len((await get('/api/catalog'))['competitions']) >= 8
        finally:
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        assert session._closed and session.metrics['worker_stop_succeeded'] is True
        assert session.metrics['caption_stop_confirmed'] is True
        assert controller.ws_manager.server is None and controller.api_manager._server is None

    asyncio.run(scenario())
