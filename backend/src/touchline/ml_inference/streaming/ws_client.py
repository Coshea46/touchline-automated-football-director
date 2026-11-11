"""Authenticated persistent replay sockets with bounded local state, no SDK RPCs."""

import asyncio
import copy
import json
import logging
import os
import time
from contextlib import suppress
from threading import Event, RLock, Thread
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import websockets

from touchline.ml_inference.config import APP_NAME
from touchline.ml_inference.streaming.client import ReplayClient
from touchline.ml_inference.streaming.contract import MAX_GPU_WORKERS

MAX_MESSAGE_BYTES = 512 * 1024


def _endpoint(value, allow_loopback):
    try:
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError
        host = parsed.hostname or ''
        secure = parsed.scheme in ('https', 'wss') and (host == 'modal.run' or host.endswith('.modal.run'))
        local = allow_loopback and parsed.scheme == 'ws' and host in ('127.0.0.1', 'localhost', '::1')
        if not (secure or local):
            raise ValueError
        path = parsed.path.rstrip('/')
        if not path.endswith('/session'):
            path += '/session'
        return urlunsplit(('wss' if secure else 'ws', parsed.netloc, path, '', ''))
    except (TypeError, ValueError):
        raise ValueError('Replay endpoint requires a Modal TLS URL; loopback is test-only') from None


def _encoded(value):
    raw = json.dumps(value, allow_nan=False, separators=(',', ':'))
    if len(raw.encode('utf-8')) > MAX_MESSAGE_BYTES:
        raise ValueError('Replay message exceeds its bounded size')
    return raw


def _reject_constant(_):
    raise ValueError('Replay messages must contain finite JSON values')


class _LocalStore:
    def __init__(self):
        self._lock, self._values, self.revision = RLock(), {}, 0

    def get(self, key, default=None):
        with self._lock:
            return copy.deepcopy(self._values.get(key, default))

    def put(self, key, value):
        with self._lock:
            if key not in self._values and len(self._values) >= 1 + MAX_GPU_WORKERS:
                raise RuntimeError('Replay local state exceeds one control and the bounded worker snapshots')
            self._values[key] = copy.deepcopy(value)
            if key.startswith('control:'):
                self.revision += 1

    def pop(self, key, default=None):
        with self._lock:
            return self._values.pop(key, default)


class _SocketHandle:
    def __init__(self, owner, spec, key):
        self._owner, self._spec, self._key = owner, copy.deepcopy(spec), key
        self._stop, self._done, self._confirmed = Event(), Event(), Event()
        self._lock = RLock()
        self._loop = self._task = self._socket = None
        self._enabled, self._started, self._error_published = True, False, False
        self._sequence = 0
        self._thread = Thread(target=self._thread_main, daemon=True, name='replay-ws-' + spec.get('shard_id', '0'))
        self._thread.start()

    def _control(self):
        control = self._owner._local.get('control:' + self._spec['session_id'], {})
        if isinstance(control.get('sources'), list):
            ids = {source['match_id'] for source in self._spec['matches']}
            control['sources'] = [source for source in control['sources']
                                  if isinstance(source, dict) and source.get('match_id') in ids]
        if self._stop.is_set():
            control = {**control, 'session_id': self._spec['session_id'], 'active': False,
                       'playing': False, 'expires_at': min(control.get('expires_at', time.time()), time.time())}
        return control

    def _publish(self, snapshot):
        with self._lock:
            if not self._enabled:
                return
            with self._owner._lock:
                active = self._owner._active
                if active is None or self._key not in active.keys:
                    return False
                index = active.keys.index(self._key)
                if (snapshot.get('session_id') == active.session_id and snapshot.get('model_version') == active.model_version
                        and snapshot.get('clock_epoch') != active.epoch):
                    return False  # A seek can overtake an in-flight old-epoch snapshot.
                if not self._owner._valid_snapshot(snapshot, active, index):
                    raise ValueError('Replay socket snapshot identity is invalid')
            self._sequence = max(self._sequence, snapshot['worker_sequence'])
            self._owner._local.put(self._key, snapshot)
            return True

    def _error(self, error_type):
        if self._stop.is_set() or self._error_published:
            return
        with self._owner._lock:
            active = self._owner._active
            if active is None:
                return
            self._error_published = True
            self._publish({'session_id': self._spec['session_id'], 'model_version': self._spec['model_version'],
                'clock_epoch': active.epoch, 'worker_sequence': self._sequence + 1,
                'score_kind': 'model' if self._spec['mode'] == 'real' else 'mock', 'state': 'error',
                'reason': 'processing_error', 'error_type': error_type, 'error_phase': 'control',
                'produced_at': time.time(), 'scores': [], 'health': {}, 'metrics': {'transport': 'websocket'}})

    async def _sender(self, socket):
        revision, stopping = None, False
        while True:
            current_revision = self._owner._local.revision
            stop = self._stop.is_set()
            if revision != current_revision or stop != stopping:
                await asyncio.wait_for(socket.send(_encoded({'type': 'control', 'control': self._control()})),
                                       timeout=self._owner.send_timeout)
                revision, stopping = current_revision, stop
            await asyncio.sleep(.05 if stop else self._owner.control_interval)

    async def _receiver(self, socket):
        while True:
            raw = await asyncio.wait_for(socket.recv(), timeout=self._owner.read_timeout)
            if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_MESSAGE_BYTES:
                raise ValueError('Replay socket message is invalid')
            value = json.loads(raw, parse_constant=_reject_constant)
            if not isinstance(value, dict):
                raise TypeError('Replay socket message must be an object')
            snapshot = value.get('snapshot') if value.get('type') == 'snapshot' else value
            if not isinstance(snapshot, dict):
                raise TypeError('Replay socket snapshot must be an object')
            if snapshot.get('state') == 'error' and 'session_id' not in snapshot:
                self._error(snapshot.get('error_type') if snapshot.get('error_type') in
                            ('ValueError', 'RuntimeError', 'TimeoutError', 'PermissionError') else 'RuntimeError')
                continue
            if (snapshot.get('state') == 'stopped' and self._stop.is_set()
                    and snapshot.get('session_id') == self._spec['session_id']
                    and snapshot.get('model_version') == self._spec['model_version']):
                # Cleanup acknowledgement belongs to this owned socket even if
                # the viewer sought to a new epoch immediately before stopping.
                self._confirmed.set()
                return
            accepted = self._publish(snapshot)
            if not accepted:
                continue
            if snapshot.get('state') == 'error':
                self._error_published = True
            if snapshot.get('state') == 'stopped':
                self._confirmed.set()
                return

    async def _main(self):
        # A private disabled wire logger prevents Authorization headers appearing
        # even when a caller enables verbose application logging.
        wire_logger = logging.getLogger(__name__ + '.wire')
        wire_logger.disabled = True
        async with self._owner._connector(self._owner._url,
                extra_headers={'Authorization': 'Bearer ' + self._owner._token,
                               'X-MatchPulse-Replay-Token': self._owner._token},
                open_timeout=self._owner.connect_timeout, close_timeout=.5,
                compression=None,
                max_size=MAX_MESSAGE_BYTES, max_queue=1, write_limit=32768,
                ping_interval=10, ping_timeout=10, logger=wire_logger) as socket:
            with self._lock:
                self._socket = socket
            await asyncio.wait_for(socket.send(_encoded({'type': 'start', 'spec': self._spec})),
                                   timeout=self._owner.send_timeout)
            self._started = True
            sender, receiver = asyncio.create_task(self._sender(socket)), asyncio.create_task(self._receiver(socket))
            try:
                done, _ = await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
            except websockets.ConnectionClosedOK as exc:
                if exc.code == 1000 and self._started and not self._error_published:
                    self._confirmed.set()
                else:
                    raise
            finally:
                sender.cancel()
                receiver.cancel()
                await asyncio.gather(sender, receiver, return_exceptions=True)

    def _thread_main(self):
        async def run():
            with self._lock:
                self._loop, self._task = asyncio.get_running_loop(), asyncio.current_task()
            if not self._stop.is_set():
                await self._main()
        try:
            asyncio.run(run())
        except asyncio.CancelledError:
            pass  # Cancellation belongs to this owned socket/connect operation.
        except Exception as exc:  # noqa: BLE001 - publish only the class, never URL/header/exception text.
            try:
                self._error(type(exc).__name__)
            except Exception:  # noqa: BLE001, S110 - a concurrently stopped local cache must not be resurrected.
                pass
        finally:
            with self._lock:
                self._loop = self._task = self._socket = None
            self._done.set()

    def cancel(self, **_):
        self._stop.set()
        with self._lock:
            loop, task, socket = self._loop, self._task, self._socket
        if loop is not None and task is not None and socket is None:
            with suppress(RuntimeError):  # It may finish and close between the lock and this callback.
                loop.call_soon_threadsafe(task.cancel)
        completed = self._done.wait(self._owner.stop_timeout)
        if not completed:
            with self._lock:
                loop, task = self._loop, self._task
            if loop is not None and task is not None:
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(task.cancel)
            self._thread.join(timeout=.25)
        with self._lock:
            self._enabled = False
        if not self._confirmed.is_set():
            raise TimeoutError('Replay WebSocket stop was not confirmed; viewer lease remains the fallback')


class ReplayWebSocketClient(ReplayClient):
    transport = 'websocket'
    stop_method = 'websocket_graceful'

    def __init__(self, url, token, *, allow_loopback=False, connect_timeout=90,
                 stop_timeout=4, send_timeout=2, read_timeout=15, control_interval=.25, connector=None):
        if not isinstance(token, str) or not token or len(token) > 4096 or '\n' in token or '\r' in token:
            raise ValueError('Replay WebSocket authentication token is required')
        self._url, self._token = _endpoint(url, allow_loopback), token
        for value in (connect_timeout, stop_timeout, send_timeout, read_timeout, control_interval):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 90:
                raise ValueError('Replay WebSocket deadlines must be positive and bounded')
        if stop_timeout > 5 or send_timeout > 5 or read_timeout > 30 or control_interval > .25:
            raise ValueError('Replay control and shutdown deadlines exceed their bounded limits')
        self.connect_timeout, self.stop_timeout = connect_timeout, stop_timeout
        self.send_timeout, self.read_timeout, self.control_interval = send_timeout, read_timeout, control_interval
        self._connector = connector or websockets.connect
        self._local = _LocalStore()
        sdk = SimpleNamespace(Dict=SimpleNamespace(from_name=lambda *a, **kw: self._local),
                              Cls=SimpleNamespace(from_name=self._worker))
        super().__init__(sdk=sdk)

    @classmethod
    def from_env(cls):
        return cls(os.environ.get('MATCHPULSE_MODAL_WS_URL'), os.environ.get('MATCHPULSE_MODAL_WS_TOKEN'))

    def _worker(self, app, name):
        if app != APP_NAME or name != 'ReplayWorker':
            raise ValueError('Replay transport only supports the independent inference worker')
        return lambda: SimpleNamespace(run=SimpleNamespace(spawn=self._spawn))

    def _spawn(self, spec):
        key = 'scores:' + spec['session_id'] + (':' + spec['shard_id'] if 'shard_id' in spec else '')
        return _SocketHandle(self, spec, key)

    def latest(self, session_id):
        # Reuse the strict parent merge with local reads only; no transient
        # executor or remote state operation is necessary on this transport.
        with self._read_lock, self._lock:
            active = self._active
            if active is None:
                return None
            if active.session_id != session_id:
                raise ValueError('Client does not own this replay session')
            snapshots = [self._local.get(key) for key in active.keys]
            if len(snapshots) == 1:
                return snapshots[0] if self._valid_snapshot(snapshots[0], active, 0) else None
            return self._merge(snapshots, active)

    def stop(self, session_id, handle):
        super().stop(session_id, handle)
        return {'confirmed': True, 'method': self.stop_method}
