"""Offline tests for integration api test_http."""


import asyncio
import threading
import pytest
from touchline.api.http import HTTPServer
from touchline.football_platform import PlatformService
from tests.support.football_platform import Transport, run, http_request


def test_actual_local_http_routes_cors_and_caption_callback_without_network():
    async def check():
        called = []

        def captions(query):
            called.append((query, threading.current_thread().name))
            return {"status": "unavailable", "match_id": query.get("match_id"), "cues": []}

        service = PlatformService()
        server = HTTPServer(service, port=0, extra_routes={"/api/captions": captions})
        await server.start()
        try:
            status, headers, catalog = await http_request(server.port, "/api/catalog?q=EPL")
            assert status == 200 and catalog["matches"][0]["competition_code"] == "PL"
            assert "Access-Control-Allow-Origin: http://localhost:3030" in headers
            assert "Cache-Control: no-store" in headers
            status, _, table = await http_request(server.port, "/api/standings?competition=PL", origin=None)
            assert status == 200 and table["status"] == "missing_key"
            status, _, caption = await http_request(server.port, "/api/captions?match_id=cam-1")
            assert status == 200 and caption["match_id"] == "cam-1"
            assert called[0][0] == {"match_id": "cam-1"}
            assert called[0][1].startswith("touchline-api")
            status, _, _ = await http_request(server.port, "/api/news", method="OPTIONS")
            assert status == 204
        finally:
            await server.close()
        assert not server._connections and service._closed
    run(check())


@pytest.mark.parametrize("target,origin,method,headers,expected", [
    ("/api/news", "https://attacker.example", "GET", "", 403),
    ("/api/news", None, "POST", "", 405),
    ("/api/standings?competition=ZZ", None, "GET", "", 400),
    ("/api/news?q=one&q=two", None, "GET", "", 400),
    ("/api/catalog?load_teams=yes", None, "GET", "", 400),
    ("https://attacker.example/api/news", None, "GET", "", 400),
    ("/api/news", None, "GET", "Content-Length: 1\r\n", 400),
    ("/api/news", None, "GET", "Origin: http://localhost:3030\r\nOrigin: http://localhost:3000\r\n", 400),
    ("/missing", None, "GET", "", 404),
])
def test_actual_http_rejects_unsafe_invalid_or_unsupported_requests(target, origin, method, headers, expected):
    async def check():
        transport = Transport()
        server = HTTPServer(PlatformService(transport=transport), port=0)
        await server.start()
        try:
            status, _, _ = await http_request(server.port, target, origin=origin, method=method, headers=headers)
            assert status == expected and transport.calls == []
        finally:
            await server.close()
    run(check())


def test_http_close_cancels_idle_clients_and_callback_bad_json_is_safe():
    async def check():
        server = HTTPServer(PlatformService(), port=0,
                                    extra_routes={"/api/bad": lambda _: {"value": float("nan")}})
        await server.start()
        _, writer = await asyncio.open_connection("127.0.0.1", server.port)
        try:
            status, _, data = await http_request(server.port, "/api/bad")
            assert status == 502 and data == {"status": "unavailable"}
            await asyncio.wait_for(server.close(), 0.5)
            assert not server._connections and not server._writers
        finally:
            writer.close()
            await writer.wait_closed()
            await server.close()
    run(check())
