"""Offline tests for integration api test_director_websocket."""


import asyncio
import json
import websockets
from touchline.replay import ReplaySession, SourceRegistry
from tests.support.replay.session import FakeBridge, configuration
from touchline.api.websocket import WebSocketManager


def test_real_socket_mode_settings_are_authoritative_and_do_not_restart_workers():
    async def scenario():
        bridge = FakeBridge()
        config = configuration(director_mode="balanced")
        session = await ReplaySession(SourceRegistry.from_env({}), config=config, bridge=bridge).initialize()
        manager = WebSocketManager(session, host="127.0.0.1", port=0)
        await manager.start()
        url = f"ws://127.0.0.1:{manager.server.sockets[0].getsockname()[1]}"
        try:
            async with websockets.connect(url) as socket:
                initial = json.loads(await socket.recv())
                assert initial["director"]["mode"] == "balanced"
                assert len(initial["matches"]) == 7
                assert [source["match_id"] for source in initial["matches"]] == [f"cam-{index}" for index in range(1, 8)]
                assert len({source["url"] for source in initial["matches"]}) == 7
                assert all(source["url"].endswith(f"match-{index}.mp4")
                           for index, source in enumerate(initial["matches"], 1))
                await socket.send(json.dumps({"type": "director_mode", "session_id": session.session_id,
                                              "mode": "relaxed"}))
                updated = json.loads(await socket.recv())
                assert updated["director"]["mode"] == "relaxed"
                assert updated["director"]["config"]["confirmations_required"] == 3
                assert updated["sequence"] > initial["sequence"]
                await socket.send(json.dumps({"type": "select", "session_id": session.session_id, "match_id": "cam-7"}))
                picked = json.loads(await socket.recv())
                assert picked["current_match_id"] == "cam-7"
                assert picked["director"]["history"][-1]["to_match_id"] == "cam-7"
                assert picked["director"]["history"][-1]["kind"] == "manual"
                await socket.send(json.dumps({"type": "director_mode", "session_id": session.session_id,
                                              "mode": ["all_action"]}))
                assert json.loads(await socket.recv())["code"] == "invalid_director_mode"
                session.connect("observer")
                assert session.receive("observer", {"type": "director_mode", "session_id": session.session_id,
                                                    "mode": "all_action"})["code"] == "viewer_in_use"
                assert not bridge.calls and session.clock_epoch == 0
        finally:
            await manager.close()
            await session.close()

    asyncio.run(scenario())
