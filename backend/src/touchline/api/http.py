"""Small bounded HTTP/1.1 GET API sharing no inference worker or socket queue."""

import asyncio
import json
import os
from urllib.parse import parse_qs, urlsplit

from .routes import ApplicationRoutes

DEFAULT_ORIGINS = frozenset({"http://localhost:3000", "http://127.0.0.1:3000",
                             "http://localhost:3030", "http://127.0.0.1:3030"})


class HTTPServer:
    MAX_HEADER_BYTES = 8192
    MAX_BODY_BYTES = 1024 * 1024

    def __init__(self, service=None, *, host="127.0.0.1", port=8031, extra_routes=None, allowed_origins=None):
        self.routes = ApplicationRoutes(service, extra_routes)
        self.service = self.routes.service
        self.host, self.port = host, port
        self.extra_routes = self.routes.extra_routes
        self.allowed_origins = DEFAULT_ORIGINS if allowed_origins is None else frozenset(allowed_origins)
        if any(not isinstance(origin, str) or urlsplit(origin).scheme not in ("http", "https")
               or not urlsplit(origin).netloc or urlsplit(origin).path or urlsplit(origin).query
               or urlsplit(origin).fragment or urlsplit(origin).username or urlsplit(origin).password
               or any(ord(char) < 32 or ord(char) > 126 for char in origin) for origin in self.allowed_origins):
            raise ValueError("invalid_api_origins")
        self._server = None
        self._connections = set()
        self._writers = set()
        self._limit = asyncio.Semaphore(16)

    @classmethod
    def from_env(cls, *, service=None, extra_routes=None, env=None):
        env = os.environ if env is None else env
        configured = env.get("MATCHPULSE_API_ALLOWED_ORIGINS")
        origins = [value.strip() for value in configured.split(",") if value.strip()] if configured else None
        return cls(service, host=env.get("MATCHPULSE_API_HOST", "127.0.0.1"),
                   port=int(env.get("MATCHPULSE_API_PORT", "8031")), extra_routes=extra_routes, allowed_origins=origins)

    async def start(self):
        if self._server:
            return
        self._server = await asyncio.start_server(self._handle, self.host, self.port, limit=self.MAX_HEADER_BYTES)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self):
        server, self._server = self._server, None
        if server:
            server.close()
        for writer in list(self._writers):
            writer.close()
        tasks = list(self._connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server:
            await server.wait_closed()
        await self.routes.close()

    async def _route(self, path, query):
        return await self.routes.dispatch(path, query)

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        if len(self._connections) >= 32:
            writer.close()
            return
        self._connections.add(task)
        self._writers.add(writer)
        origin = None
        status, payload, extra = 400, {"status": "invalid_request"}, {}
        try:
            async with self._limit:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 4)
                if len(raw) > self.MAX_HEADER_BYTES:
                    raise ValueError("invalid_request")
                lines = raw.decode("ascii").split("\r\n")
                method, target, version = lines[0].split(" ")
                if version not in ("HTTP/1.0", "HTTP/1.1") or len(target) > 2048:
                    raise ValueError("invalid_request")
                headers = {}
                for line in lines[1:-2]:
                    name, value = line.split(":", 1)
                    name = name.strip().lower()
                    if name in headers:
                        raise ValueError("invalid_request")
                    headers[name] = value.strip()
                origin = headers.get("origin")
                if origin is not None and origin not in self.allowed_origins:
                    status, payload = 403, {"status": "forbidden_origin"}
                    origin = None
                elif method == "OPTIONS":
                    status, payload = 204, None
                    extra = {"Access-Control-Allow-Methods": "GET, OPTIONS", "Access-Control-Allow-Headers": "Content-Type"}
                elif method != "GET":
                    status, payload, extra = 405, {"status": "method_not_allowed"}, {"Allow": "GET, OPTIONS"}
                elif headers.get("transfer-encoding") or headers.get("content-length", "0") != "0":
                    raise ValueError("invalid_request")
                else:
                    parsed = urlsplit(target)
                    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/"):
                        raise ValueError("invalid_request")
                    params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8, strict_parsing=True)
                    if any(len(values) != 1 for values in params.values()):
                        raise ValueError("invalid_query")
                    query = {key: values[0] for key, values in params.items()}
                    result = await asyncio.wait_for(self._route(parsed.path, query), 16)
                    status, payload = (404, {"status": "not_found"}) if result is None else (200, result)
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            status, payload = 400, {"status": "invalid_request"}
        except TimeoutError:
            status, payload = 504, {"status": "unavailable", "message": "The metadata request timed out."}
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - callbacks must not disclose credentials or paths
            status, payload = 502, {"status": "unavailable", "message": "The metadata API is temporarily unavailable."}
        finally:
            try:
                try:
                    body = b"" if payload is None else json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
                except (ValueError, TypeError):
                    status, body = 502, b'{"status":"unavailable"}'
                if len(body) > self.MAX_BODY_BYTES:
                    status, body = 502, b'{"status":"unavailable"}'
                names = {200: "OK", 204: "No Content", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                         405: "Method Not Allowed", 502: "Bad Gateway", 504: "Gateway Timeout"}
                response_headers = {"Content-Type": "application/json; charset=utf-8", "Content-Length": str(len(body)),
                                    "Cache-Control": "no-store", "Connection": "close", "X-Content-Type-Options": "nosniff",
                                    "Vary": "Origin", **extra}
                if origin:
                    response_headers["Access-Control-Allow-Origin"] = origin
                head = f"HTTP/1.1 {status} {names[status]}\r\n" + "".join(
                    f"{name}: {value}\r\n" for name, value in response_headers.items()) + "\r\n"
                writer.write(head.encode("ascii") + body)
                await asyncio.wait_for(writer.drain(), 2)
            except (OSError, TimeoutError, ValueError, asyncio.CancelledError):
                pass
            writer.close()
            self._connections.discard(task)
            self._writers.discard(writer)
