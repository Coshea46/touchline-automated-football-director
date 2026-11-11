"""Offline tests for integration ml_inference streaming test_asgi_protocol."""


import asyncio
import json
import threading
import time
import pytest
import websockets
from touchline.ml_inference.streaming.asgi import ReplayASGI
from touchline.ml_inference.streaming.ws_client import ReplayWebSocketClient


TOKEN = "local-integration-fixture.not-a-real-proxy-secret"


async def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(0.005)
    raise AssertionError("Local client/ASGI condition did not complete")


def session_spec():
    return {"session_id": "local-integration", "model_version": "gru-fixture-v1",
            "mode": "real", "checkpoint": "fixture/classifier.pt", "gpu_workers": 1,
            "max_duration_seconds": 20, "max_estimated_cost_usd": 1,
            "matches": [{"match_id": "cam-1", "source_url": "https://example.com/one.mp4", "offset_seconds": 0},
                        {"match_id": "cam-2", "source_url": "https://example.com/two.mp4", "offset_seconds": 12}]}


def control(position=0):
    return {"session_id": "local-integration", "clock_epoch": 0, "position": position,
            "playing": position > 0, "rate": 1, "active": True, "expires_at": time.time() + 5}


def adapter(app, wire, *, terminal_check=lambda: None):
    """Translate only the ASGI WebSocket events normally supplied by the server."""
    async def handler(socket):
        wire["path"] = socket.path
        wire["headers"] = dict(socket.request_headers)
        connected = False

        async def receive():
            nonlocal connected
            if not connected:
                connected = True
                return {"type": "websocket.connect"}
            try:
                raw = await socket.recv()
            except websockets.ConnectionClosed as exc:
                return {"type": "websocket.disconnect", "code": exc.code}
            message = json.loads(raw)
            wire.setdefault("received", []).append(message)
            return {"type": "websocket.receive", "text": raw}

        async def send(event):
            if event["type"] == "websocket.accept":
                wire["accepted"] = True
            elif event["type"] == "websocket.send":
                message = json.loads(event["text"])
                wire.setdefault("sent", []).append(message)
                if message.get("state") == "stopped":
                    terminal_check()
                await socket.send(event["text"])
            elif event["type"] == "websocket.close":
                await socket.close(code=event.get("code", 1000))
            else:
                raise AssertionError("Unexpected ASGI event")

        scope = {"type": "websocket", "path": socket.path,
                 "headers": [(key.lower().encode(), value.encode()) for key, value in socket.request_headers.raw_items()]}
        await app(scope, receive, send)
        wire["application_returned"] = True

    return handler


def test_actual_client_and_asgi_exchange_two_source_model_contract_then_ack_after_cleanup():
    before = set(threading.enumerate())

    async def scenario():
        cleanup_entered, allow_cleanup = threading.Event(), threading.Event()
        runner_cleaned, resources_cleaned = threading.Event(), threading.Event()
        seen_controls, wire = [], {}
        detector_fixture = object()

        class ClassifierFixture:
            """Protocol-only scores; these are not a trained GRU or quality result."""

            def predict_scores(self):
                return {"cam-1": 0.2, "cam-2": 0.8}

        class ResourcesFixture:
            stats = None

            def __enter__(self):
                self.stats = {"test_fixture": True}
                return self

            def __exit__(self, *args):
                resources_cleaned.set()

        def scorer_factory(spec):
            assert spec["mode"] == "real" and spec["checkpoint"] == "fixture/classifier.pt"
            return ClassifierFixture()

        def snapshot(spec, sequence, state, records=None):
            return {"session_id": spec["session_id"], "model_version": spec["model_version"],
                    "clock_epoch": 0, "worker_sequence": sequence, "score_kind": "model",
                    "produced_at": time.time(), "state": state, "reason": None,
                    "scores": records or [], "health": {key: {"status": "healthy"} for key in ("cam-1", "cam-2")},
                    "metrics": {"test_fixture": True}}

        def runner(spec, detector, read_control, publish, *, scorer, **kwargs):
            assert detector is detector_fixture
            published = False
            terminal = snapshot(spec, 3, "stopped")
            terminal["reason"] = "control_stopped"
            publish(snapshot(spec, 1, "ready"))
            deadline = time.monotonic() + 3
            try:
                while time.monotonic() < deadline:
                    clock = read_control()
                    seen_controls.append(clock)
                    assert clock["expires_at"] <= time.time() + 5.01
                    if not clock["active"]:
                        # The production runner also emits its terminal record
                        # before finally. ASGI must defer it until all cleanup.
                        publish(terminal)
                        break
                    if clock["position"] >= 5 and not published:
                        records = [{"session_id": spec["session_id"], "model_version": spec["model_version"],
                            "clock_epoch": 0, "match_id": match["match_id"], "sequence": 1,
                            "score": scorer.predict_scores()[match["match_id"]],
                            "observation_start": 0, "observation_end": 5,
                            "source_timestamp": match["offset_seconds"] + 5,
                            "produced_at": time.time(), "latency_seconds": 0.01, "status": "healthy"}
                            for match in spec["matches"]]
                        publish(snapshot(spec, 2, "running", records))
                        published = True
                    time.sleep(0.005)
                else:
                    raise AssertionError("Test runner did not receive stop")
            finally:
                cleanup_entered.set()
                assert allow_cleanup.wait(2), "Test cleanup release timed out"
                runner_cleaned.set()
            return terminal

        def check_terminal():
            assert runner_cleaned.is_set() and resources_cleaned.is_set()
            wire["ack_after_cleanup"] = True

        app = ReplayASGI(detector_fixture, scorer_factory, runner=runner, resource_factory=ResourcesFixture,
                         token=TOKEN, initial_timeout=1, shutdown_timeout=1)
        async with websockets.serve(adapter(app, wire, terminal_check=check_terminal), "127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/session"
            client = ReplayWebSocketClient(url, TOKEN, allow_loopback=True, control_interval=0.05,
                                           stop_timeout=1.5, connect_timeout=2, read_timeout=3)
            first = control()
            client.update_control("local-integration", first)
            owned = client.start(session_spec())
            stopping = None
            try:
                await eventually(lambda: client.latest("local-integration"))
                permitted = control(5)
                client.update_control("local-integration", permitted)
                latest = await eventually(lambda: (value if (value := client.latest("local-integration"))
                                                   and value["scores"] else None))
                assert latest["score_kind"] == "model" and latest["model_version"] == "gru-fixture-v1"
                assert {record["match_id"]: record["score"] for record in latest["scores"]} == {
                    "cam-1": 0.2, "cam-2": 0.8}
                assert [record["source_timestamp"] for record in latest["scores"]] == [5, 17]
                assert client.latest("local-integration")["worker_sequence"] == latest["worker_sequence"]
                assert wire["path"] == "/session" and wire["accepted"]
                assert wire["headers"]["authorization"] == "Bearer " + TOKEN
                assert wire["headers"]["x-matchpulse-replay-token"] == TOKEN
                assert wire["received"][0]["type"] == "start"
                assert wire["received"][1] == {"type": "control", "control": first}
                assert any(clock["position"] == 5 and clock["expires_at"] == permitted["expires_at"]
                           for clock in seen_controls)
                stopping = asyncio.create_task(asyncio.to_thread(client.stop, "local-integration", owned))
                await eventually(cleanup_entered.is_set)
                assert not stopping.done(), "Stop acknowledgement escaped before runner cleanup"
                assert not wire.get("ack_after_cleanup")
                allow_cleanup.set()
                assert await stopping == {"confirmed": True, "method": "websocket_graceful"}
                assert wire["ack_after_cleanup"]
                await eventually(lambda: wire.get("application_returned"))
                assert all(handle._done.is_set() and not handle._thread.is_alive() for handle in owned.handles)
                assert not client._local._values
            finally:
                allow_cleanup.set()
                if stopping is not None:
                    await asyncio.gather(stopping, return_exceptions=True)
                else:
                    await asyncio.to_thread(client.stop, "local-integration", owned)

    asyncio.run(scenario())
    leaked = [thread for thread in threading.enumerate() if thread not in before
              and thread.name.startswith(("replay-ws-", "replay-ws-runner"))]
    assert not leaked


def test_actual_asgi_auth_rejection_never_calls_runner_and_client_reports_unconfirmed_stop():
    async def scenario():
        wire = {}

        def forbidden(*args, **kwargs):
            raise AssertionError("Unauthorized request reached inference fixture")

        app = ReplayASGI(object(), forbidden, runner=forbidden, token=TOKEN)
        async with websockets.serve(adapter(app, wire), "127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/session"
            client = ReplayWebSocketClient(url, "incorrect-test-token", allow_loopback=True,
                                           connect_timeout=1, stop_timeout=0.2)
            client.update_control("local-integration", control())
            owned = client.start(session_spec())
            error = await eventually(lambda: client.latest("local-integration"))
            assert error["state"] == "error" and error["score_kind"] == "model"
            assert "incorrect-test-token" not in json.dumps(error)
            assert not wire.get("accepted")
            with pytest.raises(RuntimeError, match="TimeoutError"):
                await asyncio.to_thread(client.stop, "local-integration", owned)
            assert all(handle._done.is_set() for handle in owned.handles)

    asyncio.run(scenario())
