"""Bounded latest-state WebSocket delivery; handlers never call inference APIs."""

import asyncio
import json
import math
import os
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import websockets


def allowed_origins(value=None):
    """Exact browser origins only; CLI clients without Origin remain supported."""
    value = os.environ.get("MATCHPULSE_ALLOWED_ORIGINS") if value is None else value
    configured = ("http://localhost:3000", "http://127.0.0.1:3000") if value is None else value.split(",")
    result = [None]
    for origin in configured:
        origin = origin.strip()
        try:
            parsed = urlsplit(origin)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username or parsed.password or parsed.path not in ("", "/")
                    or parsed.query or parsed.fragment or "*" in origin or not parsed.port and parsed.netloc.endswith(":")):
                raise ValueError
            normalized = f"{parsed.scheme}://{parsed.netloc}"
        except ValueError as exc:
            raise ValueError("MATCHPULSE_ALLOWED_ORIGINS requires comma-separated HTTP(S) origins") from exc
        if normalized not in result:
            result.append(normalized)
    return result


@dataclass
class Connection:
    websocket: object
    pending: dict = field(default_factory=dict)
    event: asyncio.Event = field(default_factory=asyncio.Event)


class WebSocketManager:
    def __init__(self, session, host="0.0.0.0", port=8000, max_clients=16, origins=None,
                 shutdown_timeout=3):
        if (isinstance(shutdown_timeout, bool) or not isinstance(shutdown_timeout, (int, float))
                or not math.isfinite(shutdown_timeout) or not 0 < shutdown_timeout <= 30):
            raise ValueError('shutdown_timeout must be between zero and 30 seconds')
        self.session, self.host, self.port = session, host, port
        self.clients = {}
        self.server = None
        self.max_clients = max_clients
        self.shutdown_timeout = shutdown_timeout
        self._close_task = None
        self._handlers = set()
        self.origins = allowed_origins(origins)
        self.session.publish = self.broadcast
        self.metrics = {"messages_sent": 0, "delivery_seconds": 0.0}

    def _queue(self, client_id, connection, payload):
        if payload.get("type") == "session":
            payload = {**payload, "controls_enabled": self.session.controls_enabled(client_id)}
        # Only the latest state, recommendation and error wait behind a slow
        # send. Never accumulate one switch command per inference window.
        connection.pending[payload["type"]] = payload
        connection.event.set()

    def broadcast(self, payload):
        for client_id, connection in tuple(self.clients.items()):
            self._queue(client_id, connection, payload)

    async def _sender(self, client_id, connection):
        while True:
            await connection.event.wait()
            pending = sorted(connection.pending.values(), key=lambda payload: payload.get("sequence", 0))
            connection.pending.clear()
            connection.event.clear()
            for payload in pending:
                started = asyncio.get_running_loop().time()
                await asyncio.wait_for(connection.websocket.send(json.dumps(payload, allow_nan=False)), timeout=2)
                self.metrics["messages_sent"] += 1
                self.metrics["delivery_seconds"] = asyncio.get_running_loop().time() - started

    async def _reader(self, client_id, connection):
        async for raw in connection.websocket:
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                self._queue(client_id, connection, self.session._error("invalid_json"))
                continue
            error = self.session.receive(client_id, payload)
            if error:
                self._queue(client_id, connection, error)

    async def handler(self, websocket):
        if len(self.clients) >= self.max_clients:
            await websocket.close(code=1013, reason="Viewer limit reached")
            return
        client_id = id(websocket)
        connection = Connection(websocket)
        handler_task = asyncio.current_task()
        self._handlers.add(handler_task)
        self.clients[client_id] = connection
        self._queue(client_id, connection, self.session.connect(client_id))
        sender = asyncio.create_task(self._sender(client_id, connection))
        reader = asyncio.create_task(self._reader(client_id, connection))
        try:
            done, _ = await asyncio.wait((sender, reader), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except (TimeoutError, websockets.ConnectionClosed):
            pass
        finally:
            self.clients.pop(client_id, None)
            sender.cancel()
            reader.cancel()
            await asyncio.gather(sender, reader, return_exceptions=True)
            self.session.disconnect(client_id)
            try:
                await websocket.close()
            finally:
                self._handlers.discard(handler_task)

    async def start(self):
        self.server = await websockets.serve(self.handler, self.host, self.port,
                                             max_size=16384, max_queue=8, write_limit=32768,
                                             ping_interval=20, ping_timeout=20,
                                             close_timeout=2, origins=self.origins)
        return self.server

    async def serve(self):
        await self.start()
        try:
            await asyncio.Future()
        finally:
            await self.close()

    async def close(self):
        if self._close_task is None and self.server is not None:
            self._close_task = asyncio.create_task(self._close(), name='replay-websocket-shutdown')
        if self._close_task is not None:
            await asyncio.shield(self._close_task)

    async def _close(self):
        server = self.server
        if server is None:
            return
        loop = asyncio.get_running_loop()
        started = loop.time()
        connections = {id(connection.websocket): connection.websocket for connection in self.clients.values()}
        for name in ('websockets', 'connections'):
            for websocket in getattr(server, name, ()):
                connections[id(websocket)] = websocket
        # websockets11 first awaits asyncio.Server.wait_closed(), then closes
        # connections. New Python waits for accepted transports there, causing
        # a cycle while a browser stays open. Start close handshakes explicitly
        # instead of relying on that library ordering.
        server.close()
        closing = [asyncio.create_task(websocket.close(code=1001)) for websocket in connections.values()]
        waiter = asyncio.create_task(server.wait_closed())
        pending = set(closing + [waiter])
        forced = 0
        try:
            _, pending = await asyncio.wait(pending, timeout=self.shutdown_timeout)
            if pending:
                for websocket in connections.values():
                    transport = getattr(websocket, 'transport', None)
                    if transport is not None and not transport.is_closing():
                        transport.abort()
                        forced += 1
                # Python3.13+ can also abort a half-open HTTP upgrade before it
                # entered our handler or the library's public connection set.
                native_server = getattr(server, 'server', None)
                abort_clients = getattr(native_server, 'abort_clients', None)
                if callable(abort_clients):
                    abort_clients()
                for task in tuple(self._handlers):
                    task.cancel()
                for task in pending:
                    task.cancel()
                await asyncio.wait(pending | self._handlers, timeout=.1)
            for task in closing + [waiter]:
                if task.done():
                    with suppress(asyncio.CancelledError):
                        task.exception()  # Retrieve failures without exposing their text.
        finally:
            self.server = None
            self.metrics['shutdown_seconds'] = loop.time() - started
            self.metrics['shutdown_forced_connections'] = forced
