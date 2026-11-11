"""Offline tests for integration api test_websocket."""


import asyncio
import json
import pytest
import websockets
from touchline.replay import ReplaySession
from touchline.api.websocket import WebSocketManager
from tests.support.replay.session import Clock, FakeBridge, registry, configuration, scores, playback


def test_websocket_handshake_rejects_cross_site_origin_before_exposing_media_urls():
    async def scenario():
        session = await ReplaySession(registry()).initialize()
        manager = WebSocketManager(session, host="127.0.0.1", port=0, origins="https://matchpulse.example")
        await manager.start()
        url = f"ws://127.0.0.1:{manager.server.sockets[0].getsockname()[1]}"
        try:
            with pytest.raises(websockets.InvalidStatusCode) as error:
                async with websockets.connect(url, origin="https://other.example"):
                    raise AssertionError("Cross-site origin reached handler")
            assert error.value.status_code == 403
            assert not manager.clients and not session.clients
            async with websockets.connect(url, origin="https://matchpulse.example") as socket:
                assert json.loads(await socket.recv())["type"] == "session"
            async with websockets.connect(url) as socket:
                assert json.loads(await socket.recv())["type"] == "session"
        finally:
            await manager.close()
            await session.close()

    asyncio.run(scenario())


def test_real_websocket_session_manual_auto_recommendation_and_reconnect():
    async def receive_kind(socket, kind):
        for _ in range(10):
            message = json.loads(await asyncio.wait_for(socket.recv(), 2))
            if message["type"] == kind:
                return message
        raise AssertionError(f"Missing message {kind}")

    async def scenario():
        bridge = FakeBridge()
        clock = Clock()
        session = await ReplaySession(registry(), config=configuration(), bridge=bridge,
                                       now=clock, monotonic=clock, session_id="session").initialize()
        manager = WebSocketManager(session, host="127.0.0.1", port=0)
        await manager.start()
        port = manager.server.sockets[0].getsockname()[1]
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
                initial = await receive_kind(socket, "session")
                assert initial["controls_enabled"] and len(initial["matches"]) == 2
                await socket.send(json.dumps(playback(session, position=5)))
                await asyncio.sleep(0.02)
                assert session.position == 5
                assert session.accept_snapshot(scores())
                recommendation = await receive_kind(socket, "recommendation")
                assert recommendation["selected_match_id"] == "cam-2"
                assert recommendation["sequence"] > initial["sequence"]
                await socket.send(json.dumps({"type": "select", "session_id": "session", "match_id": "cam-1"}))
                selected = await receive_kind(socket, "session")
                assert selected["current_match_id"] == "cam-1"
                await socket.send(json.dumps({"type": "auto", "session_id": "session", "enabled": False}))
                assert not (await receive_kind(socket, "session"))["auto_enabled"]
                await socket.send("not-json")
                assert (await receive_kind(socket, "error"))["code"] == "invalid_json"
            async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
                restored = await receive_kind(socket, "session")
                assert restored["playback"]["position"] == 5
                assert restored["current_match_id"] == "cam-1"
                assert not restored["auto_enabled"]
            assert not bridge.calls  # Socket handlers never call Modal.
        finally:
            await manager.close()
            await session.close()

    asyncio.run(scenario())
