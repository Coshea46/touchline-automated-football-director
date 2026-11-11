"""Offline tests for integration api test_websocket_shutdown."""


import asyncio
import time
import websockets
from touchline.main import AppController
from touchline.replay import ReplaySession, SourceRegistry
from touchline.replay.config import SessionConfig
from touchline.api.websocket import WebSocketManager


async def offline_session():
    return await ReplaySession(SourceRegistry.from_env({'S3_BUCKET_NAME': 'match-switcher-poc-videos', 'MATCHPULSE_SOURCE_ACCESS': 'public', 'MATCHPULSE_MATCH_COUNT': '1'}),
                               config=SessionConfig()).initialize()


def test_cancelling_app_with_connected_viewer_finishes_and_cleans_up_session():
    async def scenario():
        controller = AppController(registry=SourceRegistry.from_env({'S3_BUCKET_NAME': 'match-switcher-poc-videos', 'MATCHPULSE_SOURCE_ACCESS': 'public', 'MATCHPULSE_MATCH_COUNT': '1'}),
                                   config=SessionConfig(), host='127.0.0.1', port=0)
        serving = asyncio.create_task(controller.start())
        while controller.ws_manager.server is None:
            await asyncio.sleep(.001)
        port = controller.ws_manager.server.sockets[0].getsockname()[1]
        socket = await websockets.connect(f'ws://127.0.0.1:{port}')
        try:
            await socket.recv()
            serving.cancel()
            done, _ = await asyncio.wait([serving], timeout=1)
            assert serving in done
            assert controller.ws_manager.server is None
            assert not controller.ws_manager.clients
            assert controller.session._closed
            assert controller.ws_manager.metrics['shutdown_forced_connections'] == 0
        finally:
            await socket.close()
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
            await controller.session.close()

    asyncio.run(scenario())


def test_shutdown_aborts_a_peer_that_does_not_read_close_frames_within_deadline():
    async def scenario():
        session = await offline_session()
        manager = WebSocketManager(session, host='127.0.0.1', port=0, shutdown_timeout=.03)
        await manager.start()
        port = manager.server.sockets[0].getsockname()[1]
        socket = await websockets.connect(f'ws://127.0.0.1:{port}', close_timeout=.05)
        try:
            await socket.recv()
            socket.transport.pause_reading()
            before = time.monotonic()
            await manager.close()
            assert time.monotonic() - before < .5
            assert manager.server is None and not manager.clients
            assert manager.metrics['shutdown_forced_connections'] == 1
            await manager.close()  # Repeated/concurrent close shares completed cleanup.
        finally:
            socket.transport.resume_reading()
            await socket.close()
            await session.close()

    asyncio.run(scenario())


def test_shutdown_bounds_a_tcp_client_that_never_completes_the_websocket_upgrade():
    async def scenario():
        session = await offline_session()
        manager = WebSocketManager(session, host='127.0.0.1', port=0, shutdown_timeout=.03)
        await manager.start()
        port = manager.server.sockets[0].getsockname()[1]
        _, writer = await asyncio.open_connection('127.0.0.1', port)
        try:
            await asyncio.sleep(.01)
            before = time.monotonic()
            await asyncio.gather(manager.close(), manager.close())
            assert time.monotonic() - before < .5
            assert manager.server is None and not manager.clients
        finally:
            writer.close()
            await writer.wait_closed()
            await session.close()

    asyncio.run(scenario())
