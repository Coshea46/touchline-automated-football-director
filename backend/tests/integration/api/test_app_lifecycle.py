"""Offline tests for integration api test_app_lifecycle."""


import asyncio
import json
import websockets
from touchline.main import AppController
from touchline.replay.config import SessionConfig
from touchline.replay import SourceRegistry


def test_platform_http_and_replay_websocket_share_a_clean_lifecycle_without_cloud(monkeypatch, tmp_path):
    for name in ("FOOTBALL_DATA_API_KEY", "GUARDIAN_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("MATCHPULSE_CAPTION_DIR", str(tmp_path))
    monkeypatch.setenv("MATCHPULSE_API_ALLOWED_ORIGINS", "http://localhost:3030")

    async def scenario():
        controller = AppController(registry=SourceRegistry.from_env({'S3_BUCKET_NAME': 'match-switcher-poc-videos', 'MATCHPULSE_SOURCE_ACCESS': 'public'}), config=SessionConfig(),
                                   host="127.0.0.1", port=0, api_port=0, api_enabled=True)
        task = asyncio.create_task(controller.start())

        async def ready():
            while controller.ws_manager.server is None:
                if task.done():
                    await task
                await asyncio.sleep(.001)

        async def get(path):
            reader, writer = await asyncio.open_connection("127.0.0.1", controller.api_manager.port)
            writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 3)
            writer.close()
            await writer.wait_closed()
            header, body = response.split(b"\r\n\r\n", 1)
            assert header.startswith(b"HTTP/1.1 200")
            return json.loads(body)

        try:
            await asyncio.wait_for(ready(), 3)
            ws_port = controller.ws_manager.server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{ws_port}") as socket:
                state = json.loads(await socket.recv())
                assert len(state["matches"]) == 7
                assert state["matches"][0]["label"] == "Chelsea vs Swansea City"
                assert state["matches"][6]["label"] == "Swansea City vs Manchester United"
                assert state["matches"][0]["metadata"]["source_key"] == "match-1.mp4"
                captions = await get("/api/captions?match_id=cam-1&source_key=match-1.mp4")
                assert captions["status"] == "unavailable" and captions["cues"] == []
                assert captions["source_identity"] == state["matches"][0]["source_identity"]
                catalog = await get("/api/catalog")
                assert len(catalog["competitions"]) >= 8
                assert not controller.session._handle
        finally:
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        assert controller.session._closed
        assert controller.ws_manager.server is None and controller.api_manager._server is None

    asyncio.run(scenario())
