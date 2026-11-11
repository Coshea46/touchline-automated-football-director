"""Authenticated, bounded WebSocket control and compact scores on one GPU input."""

import asyncio
import contextvars
import hmac
import json
import os
import time
from contextlib import suppress
from threading import Lock, Thread

from touchline.ml_inference.streaming.contract import validate_control, validate_session
from touchline.ml_inference.streaming.state import _primitive_copy

MAX_MESSAGE_BYTES = 512 * 1024


class ReplayProtocolError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _error(spec, code, error_type='ReplayProtocolError', *, clock=None, phase=None):
    return {
        'session_id': spec.get('session_id'), 'model_version': spec.get('model_version'),
        'clock_epoch': (clock or {}).get('clock_epoch', 0), 'worker_sequence': 1,
        'state': 'error', 'reason': code, 'error_code': code,
        'error_type': error_type, 'error_phase': phase,
        'score_kind': 'model' if spec.get('mode') == 'real' else 'mock',
        'produced_at': time.time(), 'scores': [], 'health': {},
    }


def checkpoint_error(spec, exc, phase, clock=None):
    """Map only application-owned checkpoint errors; never serialize SDK text."""
    if phase not in ('resolve', 'volume_reload', 'load', 'validate'):
        phase = 'load'
    codes = {
        'Checkpoint must remain inside inference-only storage': 'checkpoint_path_escape',
        'Portable checkpoint must be a nonempty file no larger than 64 MiB': 'checkpoint_file_invalid',
        'Portable checkpoint exceeds 64 MiB': 'checkpoint_file_too_large',
        'Portable classifier metadata must be a mapping': 'checkpoint_metadata_invalid',
        'Replay requires a portable full-audio matchpulse-215-v1 checkpoint': 'checkpoint_schema_mismatch',
        'Replay requires the full-audio hidden-size-64 GRU': 'checkpoint_architecture_mismatch',
        'Loaded replay model must be an eval-mode one-layer unidirectional GRU': 'checkpoint_model_structure',
        'Replay requires saved training-only normalization provenance': 'checkpoint_normalization_provenance',
        'Replay normalization must describe all 215 finite channels': 'checkpoint_normalization_invalid',
        'Replay requires dataset audit SHA256 provenance': 'checkpoint_dataset_provenance',
        'Replay requires a validation-selected operating threshold': 'checkpoint_threshold_invalid',
        'Invalid replay model version': 'checkpoint_version_invalid',
        'Classifier does not implement the five-second MatchPulse schema': 'checkpoint_schema_mismatch',
        'Production requires a full-audio 215-channel classifier bundle': 'checkpoint_audio_schema',
    }
    result = _error(spec, 'checkpoint_unavailable', type(exc).__name__, clock=clock, phase=phase)
    result['error_code'] = codes.get(str(exc), 'checkpoint_' + phase + '_failed')
    return result


def _decode(event):
    if event.get('type') == 'websocket.disconnect':
        raise ReplayProtocolError('disconnected')
    if event.get('type') != 'websocket.receive':
        raise ReplayProtocolError('message_type_invalid')
    raw = event.get('text')
    if raw is None:
        raw = event.get('bytes')
    if not isinstance(raw, (str, bytes)):
        raise ReplayProtocolError('json_invalid')
    if len(raw.encode('utf-8') if isinstance(raw, str) else raw) > MAX_MESSAGE_BYTES:
        raise ReplayProtocolError('message_too_large')
    try:
        value = json.loads(raw)
        value = _primitive_copy(value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ReplayProtocolError('json_invalid') from None
    if type(value) is not dict:
        raise ReplayProtocolError('message_object_required')
    return value


class _Control:
    def __init__(self, spec, initial):
        self.spec, self._value, self._lock = spec, initial, Lock()

    def read(self):
        with self._lock:
            return _primitive_copy(self._value)

    def update(self, value):
        try:
            validate_control(self.spec['session_id'], value)
            candidate = _primitive_copy(value)
            if not candidate['active']:
                # Revocation is authoritative even when a generic client sends
                # its zero-position/zero-epoch stop packet. Never rewind or
                # renew the last permitted clock while stopping this input.
                with self._lock:
                    self._value = {**self._value, 'session_id': self.spec['session_id'],
                        'active': False, 'playing': False,
                        'expires_at': min(self._value.get('expires_at', candidate['expires_at']),
                                          candidate['expires_at'])}
                return
            if 'sources' in candidate:
                sources = candidate['sources']
                if type(sources) is not list or len(sources) > len(self.spec['matches']):
                    raise ValueError('Invalid source updates')
                registered = {source['match_id']: source for source in self.spec['matches']}
                updates = {}
                for source in sources:
                    if type(source) is not dict or source.get('match_id') not in registered:
                        raise ValueError('Unknown source update')
                    key = source['match_id']
                    if key in updates:
                        raise ValueError('Duplicate source update')
                    updates[key] = {**registered[key], **source}
                validate_session({**self.spec, 'matches': [updates.get(k, v) for k, v in registered.items()]})
            with self._lock:
                old_epoch, new_epoch = self._value.get('clock_epoch', 0), candidate.get('clock_epoch', 0)
                if new_epoch < old_epoch or (new_epoch == old_epoch
                        and candidate['position'] < self._value['position'] - .001):
                    raise ValueError('Replay clock regressed')
                self._value = candidate
        except (ValueError, TypeError, KeyError):
            raise ReplayProtocolError('control_invalid') from None

    def stop(self):
        with self._lock:
            self._value = {**self._value, 'active': False, 'playing': False}


class _Outbox:
    """One pending snapshot and one scheduled cross-thread callback, not a log."""
    def __init__(self, loop):
        self.loop, self.queue = loop, asyncio.Queue(maxsize=1)
        self._lock, self._pending, self._scheduled = Lock(), None, False
        self._closed = False
        self._publications = self._coalesced = self.sent = 0
        self.send_seconds = 0.0

    def publish(self, value):
        value = _primitive_copy(value)
        with self._lock:
            if self._closed:
                return
            self._publications += 1
            self._coalesced += int(self._pending is not None)
            self._pending = value
            if not self._scheduled:
                self._scheduled = True
                self.loop.call_soon_threadsafe(self._drain)

    def _drain(self):
        with self._lock:
            value, self._pending, self._scheduled = self._pending, None, False
            if self._closed:
                return
        if value is not None:
            if self.queue.full():
                self.queue.get_nowait()
                with self._lock:
                    self._coalesced += 1
            self.queue.put_nowait(value)

    @property
    def stats(self):
        with self._lock:
            return {'transport': 'websocket', 'publications': self._publications,
                    'coalesced_snapshots': self._coalesced, 'max_pending': 1,
                    'sent': self.sent, 'send_seconds': self.send_seconds}

    def close(self):
        with self._lock:
            self._closed, self._pending = True, None


class ReplayASGI:
    def __init__(self, detector, scorer_factory, *, detector_config=None, runtime=None,
                 runner=None, token=None, phase_getter=None, resource_factory=None,
                 initial_timeout=5, shutdown_timeout=5, send_timeout=2):
        self.detector, self.scorer_factory = detector, scorer_factory
        self.detector_config, self.runtime = detector_config, runtime
        self.runner, self.token = runner, token
        self.phase_getter = phase_getter or (lambda: 'load')
        self.resource_factory = resource_factory
        self.initial_timeout, self.shutdown_timeout, self.send_timeout = initial_timeout, shutdown_timeout, send_timeout
        self._session_gate = Lock()

    def _secret(self):
        return self.token if self.token is not None else os.environ.get('MATCHPULSE_REPLAY_WS_TOKEN', '')

    def _authorized(self, scope):
        headers = scope.get('headers', [])
        custom = [v for k, v in headers if k.lower() == b'x-matchpulse-replay-token']
        bearer = [v for k, v in headers if k.lower() == b'authorization']
        if custom:
            provided = custom[0] if len(custom) == 1 else b''
        else:
            provided = bearer[0][7:] if len(bearer) == 1 and bearer[0].startswith(b'Bearer ') else b''
        expected = self._secret()
        return bool(expected and provided and len(provided) <= 4096
                    and hmac.compare_digest(provided, expected.encode('utf-8')))

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                event = await receive()
                if event['type'] == 'lifespan.startup':
                    await send({'type': 'lifespan.startup.complete'})
                elif event['type'] == 'lifespan.shutdown':
                    await send({'type': 'lifespan.shutdown.complete'})
                    return
        if scope['type'] != 'websocket':
            await send({'type': 'http.response.start', 'status': 404, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})
            return
        if scope.get('path') != '/session' or not self._authorized(scope):
            await send({'type': 'websocket.close', 'code': 1008})
            return
        if not self._session_gate.acquire(blocking=False):
            await send({'type': 'websocket.close', 'code': 1013})
            return
        spec, state, outbox, thread = {}, None, None, None
        reader_task = writer_task = done_task = None
        done, disconnected, accepted = asyncio.Event(), False, False
        shutdown_deadline = None
        loop = asyncio.get_running_loop()

        async def join_runner():
            nonlocal shutdown_deadline
            if thread is None or done.is_set():
                return
            if shutdown_deadline is None:
                shutdown_deadline = time.monotonic() + self.shutdown_timeout
            remaining = shutdown_deadline - time.monotonic()
            if remaining > 0:
                with suppress(TimeoutError):
                    await asyncio.wait_for(done.wait(), remaining)

        async def send_record(record):
            text = json.dumps(_primitive_copy(record), allow_nan=False, separators=(',', ':'))
            await asyncio.wait_for(send({'type': 'websocket.send', 'text': text}), self.send_timeout)

        async def writer():
            while True:
                record = await outbox.queue.get()
                before = time.perf_counter()
                await send_record(record)
                outbox.sent += 1
                outbox.send_seconds += time.perf_counter() - before
                if record.get('state') in ('stopped', 'error'):
                    return record['state']

        async def reader():
            while True:
                event = await receive()
                if event.get('type') == 'websocket.disconnect':
                    return 'disconnected'
                if not self._authorized(scope):
                    raise ReplayProtocolError('authorization_revoked')
                message = _decode(event)
                if message.get('type') != 'control':
                    raise ReplayProtocolError('control_message_required')
                state.update(message.get('control'))

        def execute():
            terminal = None
            try:
                try:
                    scorer = self.scorer_factory(spec)
                except Exception as exc:  # noqa: BLE001 -- exact error classes/codes only.
                    terminal = checkpoint_error(spec, exc, self.phase_getter(), state.read())
                    return
                runner = self.runner
                if runner is None:
                    from touchline.ml_inference.streaming.runner import run_replay_session

                    runner = run_replay_session
                resource_factory = self.resource_factory
                if resource_factory is None:
                    from touchline.ml_inference.streaming.metrics import ResourceSampler

                    resource_factory = ResourceSampler

                def publish(record):
                    # run_replay_session publishes stopped BEFORE its finally.
                    # Hold every terminal record until it has actually returned.
                    nonlocal terminal
                    if record.get('state') in ('stopped', 'error'):
                        terminal = _primitive_copy(record)
                    else:
                        outbox.publish(record)

                with resource_factory() as resources:
                    result = runner(spec, self.detector, state.read, publish, scorer=scorer,
                        detector_config=self.detector_config, runtime=self.runtime,
                        state_metrics=lambda: {**outbox.stats, 'resources': resources.stats})
                terminal = result if type(result) is dict else terminal
                if terminal is None:
                    terminal = _error(spec, 'runner_result_invalid', 'RuntimeError', clock=state.read())
            except Exception as exc:  # noqa: BLE001 -- never expose decoder/source credentials.
                terminal = _error(spec, 'runner_failed', type(exc).__name__, clock=state.read(), phase='runner')
            finally:
                # Runner/resource cleanup has finished before a stopped ACK can
                # enter the socket. A disconnected/expired viewer owns no input.
                if terminal is not None:
                    with suppress(RuntimeError, TypeError, ValueError):
                        outbox.publish(terminal)
                self._session_gate.release()
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(done.set)

        try:
            event = await asyncio.wait_for(receive(), self.initial_timeout)
            if event.get('type') != 'websocket.connect':
                raise ReplayProtocolError('connect_required')
            await send({'type': 'websocket.accept'})
            accepted = True
            message = _decode(await asyncio.wait_for(receive(), self.initial_timeout))
            if message.get('type') != 'start' or type(message.get('spec')) is not dict:
                raise ReplayProtocolError('start_spec_required')
            spec = message['spec']
            try:
                validate_session(spec)
            except (ValueError, TypeError, KeyError):
                raise ReplayProtocolError('session_invalid') from None
            message = _decode(await asyncio.wait_for(receive(), self.initial_timeout))
            if message.get('type') != 'control':
                raise ReplayProtocolError('initial_control_required')
            state = _Control(spec, {'position': 0, 'clock_epoch': 0})
            state.update(message.get('control'))
            outbox = _Outbox(loop)
            reader_task, writer_task = asyncio.create_task(reader()), asyncio.create_task(writer())
            context = contextvars.copy_context()
            thread = Thread(target=context.run, args=(execute,), daemon=True, name='replay-ws-runner')
            thread.start()
            done_task = asyncio.create_task(done.wait())
            completed, _ = await asyncio.wait((reader_task, writer_task, done_task), return_when=asyncio.FIRST_COMPLETED)
            if reader_task in completed:
                disconnected = reader_task.result() == 'disconnected'
                state.stop()
                if disconnected:
                    return
            if writer_task in completed:
                terminal_state = writer_task.result()
            else:
                terminal_state = await asyncio.wait_for(writer_task, self.send_timeout + 1)
            await send({'type': 'websocket.close', 'code': 1000 if terminal_state == 'stopped' else 1011})
        except ReplayProtocolError as exc:
            disconnected = exc.code == 'disconnected'
            if not disconnected and accepted:
                if state is not None:
                    state.stop()
                if writer_task is not None:
                    writer_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await writer_task
                if thread is not None:
                    await join_runner()
                await send_record(_error(spec, exc.code, clock=state.read() if state else None))
                await send({'type': 'websocket.close', 'code': 1008})
        except TimeoutError:
            if accepted:
                with suppress(Exception):
                    await send_record(_error(spec, 'session_io_timeout', 'TimeoutError'))
                    await send({'type': 'websocket.close', 'code': 1011})
        except asyncio.CancelledError:
            disconnected = True
            raise
        except Exception as exc:  # noqa: BLE001 -- socket failures contain no public exception text.
            if accepted and not disconnected:
                with suppress(Exception):
                    await send_record(_error(spec, 'websocket_failed', type(exc).__name__))
                    await send({'type': 'websocket.close', 'code': 1011})
        finally:
            if state is not None:
                state.stop()
            for task in (reader_task, writer_task, done_task):
                if task is not None:
                    task.cancel()
            for task in (reader_task, writer_task, done_task):
                if task is not None:
                    with suppress(asyncio.CancelledError, Exception):
                        await task
            if thread is not None:
                with suppress(asyncio.CancelledError):
                    await join_runner()
            else:
                self._session_gate.release()
            if outbox is not None:
                outbox.close()
