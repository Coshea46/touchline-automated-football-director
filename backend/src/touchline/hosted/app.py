"""Native ASGI transport for independently owned public replay sessions."""

import asyncio
import json
import logging
import os
import re
import time
from contextlib import suppress
from urllib.parse import parse_qs

from touchline.api.routes import ApplicationRoutes
from touchline.api.websocket import allowed_origins
from touchline.football_platform.service import PlatformService
from touchline.hosted.runtime import create_session, create_source_access
from touchline.hosted.sessions import SessionDenied, SessionManager, public_code
from touchline.storage import StorageAuthorizationError

logger = logging.getLogger(__name__)

CLOSE_CODES = {"invalid_hello": 4400, "resume_rejected": 4403,
    "session_expired": 4404, "hello_timeout": 4408, "session_replaced": 4409,
    "capacity_reached": 4413, "rate_limited": 4429, "server_unavailable": 1013}


def _json(raw):
    def reject(_):
        raise ValueError("nonfinite_json")
    return json.loads(raw, parse_constant=reject)


class Socket:
    def __init__(self, send):
        self.send = send
        self.pending = {}
        self.event = asyncio.Event()
        self.lock = asyncio.Lock()
        self.closed = False
        self.client_id = None

    def queue(self, payload):
        if not self.closed:
            self.pending[payload["type"]] = payload
            self.event.set()

    async def write(self, payload):
        async with self.lock:
            if not self.closed:
                await asyncio.wait_for(self.send({"type": "websocket.send",
                    "text": json.dumps(payload, allow_nan=False, separators=(",", ":"))}), 2)

    async def sender(self):
        while not self.closed:
            await self.event.wait()
            pending = sorted(self.pending.values(), key=lambda value: value.get("sequence", 0))
            self.pending.clear()
            self.event.clear()
            for payload in pending:
                await self.write(payload)

    async def close(self, code="server_unavailable"):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.event.set()  # Retire idle sender even if the peer never acknowledges close.
            await asyncio.wait_for(self.send({"type": "websocket.close",
                "code": CLOSE_CODES.get(code, 1001), "reason": code}), 2)


class HostedApplication:
    def __init__(self, env, admission, session_factory=None, routes=None, *, now=None, source_access=None):
        self.env, self.admission = env, admission
        configured_origins = env.get("MATCHPULSE_ALLOWED_ORIGINS")
        if not configured_origins:
            raise ValueError("Hosted service requires explicit MATCHPULSE_ALLOWED_ORIGINS")
        self.origins = {origin for origin in allowed_origins(configured_origins) if origin}
        self.source_access = (source_access if source_access is not None else
                              create_source_access(env) if session_factory is None else None)
        factory = session_factory or (lambda session_id, guard: create_session(
            env, session_id, guard, source_access=self.source_access))
        self.sessions = SessionManager(factory, admission, **({"now": now} if now else {}))
        self.routes = routes or ApplicationRoutes(service=PlatformService.from_env(env))
        self._sweeper = None
        self._handlers = set()
        self._closed = False
        self._caption_limit = asyncio.Semaphore(4)
        self._source_warm_task = None
        self.source_warm_error = None
        self.source_warm_seconds = 0.0
        self.source_access_close_confirmed = None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
        elif scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
        elif scope["type"] == "http":
            await self._http(scope, receive, send)

    def _ensure_sweeper(self):
        if self._sweeper is None and not self._closed:
            self._sweeper = asyncio.create_task(self._sweep(), name="visitor-expiry")

    async def _sweep(self):
        while True:
            await self.sessions.sweep()
            await asyncio.sleep(0.5)

    async def _warm_source_access(self):
        if self.source_access is None:
            return
        started = time.monotonic()
        if self._source_warm_task is None:
            self._source_warm_task = asyncio.create_task(asyncio.to_thread(self.source_access.warm))
            def finished(task):
                if not task.cancelled():
                    task.exception()  # Retrieve late failures after lifespan cancellation.
            self._source_warm_task.add_done_callback(finished)
        try:
            await asyncio.shield(self._source_warm_task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            label = exc.code if isinstance(exc, StorageAuthorizationError) else type(exc).__name__
            if not isinstance(label, str) or re.fullmatch(r'[A-Za-z_]{1,64}', label) is None:
                label = "source_warm_unavailable"
            self.source_warm_error = label
            logger.warning("Source authorization warmup unavailable (%s)", label)
            self._source_warm_task = None
        else:
            self.source_warm_error = None
        finally:
            self.source_warm_seconds = time.monotonic() - started

    async def _lifespan(self, receive, send):
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                await self._warm_source_access()
                warm_admission = getattr(self.admission, "warm", None)
                if warm_admission is not None:
                    try:
                        await warm_admission()
                    except Exception:
                        # A paid public service must not accept traffic before
                        # its durable financial store is ready. No SDK detail
                        # or credential may reach the ASGI failure message.
                        with suppress(Exception):
                            await self.close()
                        await send({"type": "lifespan.startup.failed",
                                    "message": "Admission initialization unavailable"})
                        return
                self._ensure_sweeper()
                await send({"type": "lifespan.startup.complete"})
            elif event["type"] == "lifespan.shutdown":
                await self.close()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
        # Cancel a visitor still initializing native source access too. Its
        # initialize() cleanup queues close after any outstanding SDK thread.
        handlers = tuple(self._handlers)
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        try:
            await self.sessions.close()
            await self.routes.close()
            close_admission = getattr(self.admission, "close", None)
            if close_admission is not None:
                await close_admission()
        finally:
            if self.source_access is not None:
                try:
                    self.source_access_close_confirmed = await asyncio.to_thread(self.source_access.close)
                except Exception:
                    self.source_access_close_confirmed = False
                    logger.warning("Source authorization cleanup unavailable")

    async def _message(self, receive):
        event = await receive()
        if event["type"] == "websocket.disconnect":
            return None
        raw = event.get("text")
        if event["type"] != "websocket.receive" or not isinstance(raw, str) or len(raw.encode()) > 16384:
            raise SessionDenied("invalid_hello")
        try:
            return _json(raw)
        except (ValueError, TypeError):
            raise SessionDenied("invalid_hello") from None

    async def _websocket(self, scope, receive, send):
        headers = self._headers(scope)
        if (scope.get("path") != "/ws" or scope.get("query_string")
                or headers.get("origin") not in self.origins or self._closed):
            await send({"type": "websocket.close", "code": 4403})
            return
        if (await receive())["type"] != "websocket.connect":
            return
        await send({"type": "websocket.accept"})
        self._ensure_sweeper()
        socket, visitor = Socket(send), None
        handler = asyncio.current_task()
        self._handlers.add(handler)
        tasks = []
        try:
            # Gateway ASGI client address is authoritative. Browser-controlled
            # Forwarded/X-Forwarded-For headers never influence the quota key.
            peer = (scope.get("client") or ("unknown", 0))[0]
            await self.admission.allow_connection(peer)
            try:
                hello = await asyncio.wait_for(self._message(receive), 5)
            except TimeoutError:
                raise SessionDenied("hello_timeout") from None
            if hello is None:
                return
            visitor, auth = await self.sessions.attach(hello, peer, socket)
            await socket.write(auth)
            socket.queue(self.sessions.snapshot(visitor, visitor.session.snapshot(socket.client_id)))
            sender = asyncio.create_task(socket.sender())

            async def reader():
                while True:
                    message = await self._message(receive)
                    if message is None:
                        return
                    error = await self.sessions.receive(visitor, socket, message, defer_paid_start=True)
                    if error:
                        socket.queue(error)

            tasks = [sender, asyncio.create_task(reader())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = public_code(getattr(exc, "code", "server_unavailable"))
            if code not in CLOSE_CODES:
                code = "server_unavailable"
            error = {"type": "error", "code": code}
            retry = getattr(exc, "retry_after", None)
            if isinstance(retry, (int, float)):
                error["retry_after"] = retry
            with suppress(Exception):
                await socket.write(error)
                await socket.close(code)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if visitor is not None:
                self.sessions.detach(visitor, socket)
            with suppress(Exception):
                await socket.close("server_shutdown" if self._closed else "client_disconnected")
            self._handlers.discard(handler)

    @staticmethod
    def _headers(scope):
        headers = {}
        for key, value in scope.get("headers", ()):
            name = key.decode("latin1").lower()
            if name in headers:
                return {}  # Ambiguous origins are rejected.
            headers[name] = value.decode("latin1")
        return headers

    async def _captions(self, query):
        if set(query) - {"match_id", "source_key", "source_identity"}:
            raise ValueError("invalid_caption_query")
        # Recorded source-bound tracks are shared recording data, never a way
        # to create a session, change its clock or start transcription.
        visitor = next(iter(self.sessions.visitors.values()), None)
        if visitor is None:
            return {"status": "unavailable", "reason": "missing_track", "cues": []}
        async with self._caption_limit, visitor.caption_lock:
            def read():
                if visitor.caption_service is None:
                    from touchline.captions import CaptionService
                    from touchline.match_metadata import METADATA_PATH
                    visitor.caption_service = CaptionService.from_metadata(METADATA_PATH,
                        sources=tuple(source for source in visitor.session.sources if source.metadata),
                        source_provider=lambda: visitor.session.sources)
                return visitor.caption_service.get(query.get("match_id", ""),
                    query.get("source_key"), query.get("source_identity"))
            return await asyncio.to_thread(read)

    async def _http(self, scope, receive, send):
        headers = self._headers(scope)
        origin = headers.get("origin")
        status, payload = 200, None
        extra = []
        try:
            await self.admission.allow_http((scope.get("client") or ("unknown", 0))[0])
            if origin is not None and origin not in self.origins:
                status, payload, origin = 403, {"status": "forbidden_origin"}, None
            elif scope["method"] == "OPTIONS":
                status = 204
                extra = [(b"access-control-allow-methods", b"GET, OPTIONS"),
                         (b"access-control-allow-headers", b"Content-Type")]
            elif scope["method"] != "GET":
                status, payload = 405, {"status": "method_not_allowed"}
            elif scope.get("path") == "/healthz":
                payload = {"status": "ok"}
            else:
                raw_query = scope.get("query_string", b"")
                if len(raw_query) > 2048:
                    raise ValueError
                parsed = parse_qs(raw_query.decode("ascii"), keep_blank_values=True,
                    strict_parsing=True, max_num_fields=8)
                if any(len(values) != 1 for values in parsed.values()):
                    raise ValueError
                query = {key: values[0] for key, values in parsed.items()}
                route = (self._captions(query) if scope["path"] == "/api/captions"
                         else self.routes.dispatch(scope["path"], query))
                payload = await asyncio.wait_for(route, 16)
                if payload is None:
                    status, payload = 404, {"status": "not_found"}
        except ValueError:
            status, payload = 400, {"status": "invalid_request"}
        except TimeoutError:
            status, payload = 504, {"status": "unavailable"}
        except Exception as exc:
            if public_code(getattr(exc, "code", None)) == "rate_limited":
                retry = max(1, int(getattr(exc, "retry_after", 60)))
                status, payload = 429, {"status": "rate_limited", "retry_after": retry}
                extra = [(b"retry-after", str(retry).encode())]
            else:
                status, payload = 502, {"status": "unavailable"}
        try:
            body = b"" if payload is None else json.dumps(payload, allow_nan=False).encode()
            if len(body) > 1024 * 1024:
                raise ValueError
        except (ValueError, TypeError):
            status, body = 502, b'{"status":"unavailable"}'
        response_headers = [(b"content-type", b"application/json; charset=utf-8"),
            (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
            (b"vary", b"Origin"), *extra]
        if origin:
            response_headers.append((b"access-control-allow-origin", origin.encode()))
        await send({"type": "http.response.start", "status": status, "headers": response_headers})
        await send({"type": "http.response.body", "body": body})


def create_app(*, env=None, admission=None, session_factory=None, routes=None, now=None, source_access=None):
    """No listening sockets, provider requests or cloud work during construction."""
    env = os.environ if env is None else env
    if admission is None:
        from touchline.hosted.admission import AdmissionPolicy
        admission = AdmissionPolicy(env=env)
    return HostedApplication(env, admission, session_factory, routes, now=now, source_access=source_access)
