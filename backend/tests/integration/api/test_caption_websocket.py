"""Offline tests for integration api test_caption_websocket."""


import asyncio
import json
import websockets
from tests.support.replay.session import playback
from touchline.api.websocket import WebSocketManager
from tests.support.captions.session import setup, event


def test_actual_socket_caption_control_and_events_share_bounded_latest_delivery():
    async def check():
        session, worker, _, _ = await setup()
        manager = WebSocketManager(session, host="127.0.0.1", port=0, origins="http://localhost:3030")
        await manager.start()
        port = manager.server.sockets[0].getsockname()[1]
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}", origin="http://localhost:3030") as socket:
                initial = json.loads(await socket.recv())
                # setup's synthetic owner is removed so this actual socket owns
                # playback and can consent to the provider-free fake worker.
                session.disconnect("owner")
                await socket.send(json.dumps(playback(session, position=5)))
                await socket.send(json.dumps({"type": "captions", "session_id": "session", "enabled": True}))
                for _ in range(10):
                    response = json.loads(await asyncio.wait_for(socket.recv(), 1))
                    if response.get("captions", {}).get("enabled"):
                        break
                assert response["captions"]["enabled"]
                assert initial["session_id"] == "session" and worker.updates
                for index in range(30):
                    assert session._accept_caption_event(event(session, text=f"fixture {index}"))
                for connection in manager.clients.values():
                    assert len(connection.pending) <= 4  # session/recommendation/error/caption only
                for _ in range(10):
                    latest = json.loads(await asyncio.wait_for(socket.recv(), 1))
                    if latest.get("type") == "caption":
                        break
                assert latest["text"] == "fixture 29"
        finally:
            await manager.close()
            await session.close()
    asyncio.run(check())
