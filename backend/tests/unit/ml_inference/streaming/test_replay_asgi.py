"""Offline tests for unit ml_inference streaming test_replay_asgi."""


import asyncio
import contextvars
import copy
import json
import time
from contextlib import nullcontext
from threading import Event
from types import SimpleNamespace
import numpy as np
import pytest
from touchline.ml_inference.streaming.asgi import MAX_MESSAGE_BYTES, ReplayASGI, _Control


TOKEN = 'test-replay-authorization'


def spec():
    return {'session_id': 'ws-unit', 'mode': 'features_mock', 'model_version': 'mock-v1',
            'max_duration_seconds': 10, 'max_estimated_cost_usd': 1,
            'matches': [{'match_id': 'match-a', 'source_url': 'https://example.test/match.mp4'}]}


def control(**updates):
    return {'session_id': 'ws-unit', 'active': True, 'playing': True,
            'clock_epoch': 0, 'position': 3.25, 'expires_at': time.time() + 2, **updates}


def record(session, state, **updates):
    return {'session_id': session['session_id'], 'model_version': session['model_version'],
            'clock_epoch': 0, 'worker_sequence': 1, 'state': state, 'score_kind': 'mock',
            'produced_at': time.time(), 'scores': [], 'health': {}, **updates}


class Socket:
    def __init__(self, *, headers=None):
        self.scope = {'type': 'websocket', 'path': '/session',
                      'headers': headers if headers is not None else [(b'authorization', b'Bearer ' + TOKEN.encode())]}
        self.incoming = asyncio.Queue()
        self.outgoing = []
        self.send_gate = None
        self.send_entered = Event()

    async def receive(self):
        return await self.incoming.get()

    async def send(self, value):
        if value['type'] == 'websocket.send' and self.send_gate is not None:
            self.send_entered.set()
            await self.send_gate.wait()
        self.outgoing.append(copy.deepcopy(value))

    def feed(self, value):
        self.incoming.put_nowait({'type': 'websocket.receive', 'text': json.dumps(value)})

    @property
    def records(self):
        return [json.loads(item['text']) for item in self.outgoing if item['type'] == 'websocket.send']


def app(runner, **kwargs):
    return ReplayASGI(None, lambda session: object(), runner=runner, token=TOKEN,
                      resource_factory=lambda: nullcontext(SimpleNamespace(stats={})),
                      initial_timeout=.1, shutdown_timeout=.3, send_timeout=.3, **kwargs)


async def begin(application, socket=None, *, initial=None):
    socket = socket or Socket()
    socket.incoming.put_nowait({'type': 'websocket.connect'})
    socket.feed({'type': 'start', 'spec': spec()})
    socket.feed({'type': 'control', 'control': initial or control()})
    task = asyncio.create_task(application(socket.scope, socket.receive, socket.send))
    return socket, task


async def wait_until(condition, timeout=1):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError('ASGI test condition timed out')
        await asyncio.sleep(.001)


@pytest.mark.parametrize('headers', [[], [(b'authorization', b'Bearer wrong')],
    [(b'authorization', b'Bearer ' + TOKEN.encode()), (b'authorization', b'Bearer ' + TOKEN.encode())],
    [(b'x-matchpulse-replay-token', b'wrong'), (b'authorization', b'Bearer ' + TOKEN.encode())]])
def test_authentication_rejects_before_accept_or_starting_a_runner(headers):
    async def run():
        application = app(lambda *args, **kwargs: pytest.fail('Unauthorized runner'))
        socket = Socket(headers=headers)
        await application(socket.scope, socket.receive, socket.send)
        assert socket.outgoing == [{'type': 'websocket.close', 'code': 1008}]
    asyncio.run(run())


def test_custom_token_header_supports_a_proxy_consuming_the_authorization_header():
    async def run():
        def runner(session, detector, reader, publish, **kwargs):
            return record(session, 'stopped', reason='control_stopped')
        socket = Socket(headers=[(b'x-matchpulse-replay-token', TOKEN.encode())])
        socket, task = await begin(app(runner), socket)
        await asyncio.wait_for(task, 1)
        assert socket.records[-1]['state'] == 'stopped'
        assert socket.outgoing[-1] == {'type': 'websocket.close', 'code': 1000}
    asyncio.run(run())


@pytest.mark.parametrize('message,reason', [
    ('<script>not-json</script>', 'json_invalid'),
    ('{"type":"start","spec":"<script>bad</script>"}', 'start_spec_required'),
    ('{"type":"start","spec":NaN}', 'json_invalid'),
    ('x' * (MAX_MESSAGE_BYTES + 1), 'message_too_large'),
])
def test_bad_json_html_and_oversized_messages_never_start_gpu_work(message, reason):
    async def run():
        application = app(lambda *args, **kwargs: pytest.fail('Invalid session runner'))
        socket = Socket()
        socket.incoming.put_nowait({'type': 'websocket.connect'})
        socket.incoming.put_nowait({'type': 'websocket.receive', 'text': message})
        await application(socket.scope, socket.receive, socket.send)
        assert socket.records[-1]['reason'] == reason
        assert '<script>' not in str(socket.records)
    asyncio.run(run())


def test_initial_control_is_required_and_cross_session_controls_are_rejected():
    async def run():
        socket, task = await begin(app(lambda *a, **k: pytest.fail('Bad control')),
                                   initial=control(session_id='other-session'))
        await asyncio.wait_for(task, 1)
        assert socket.records[-1]['reason'] == 'control_invalid'
    asyncio.run(run())


def test_lease_expires_without_messages_and_clock_is_never_extrapolated():
    async def run():
        seen = []
        initial = control(expires_at=time.time() + .04)

        def runner(session, detector, reader, publish, **kwargs):
            while True:
                value = reader()
                seen.append(value)
                if value['expires_at'] < time.time():
                    return record(session, 'stopped', reason='lease_expired')
                time.sleep(.001)

        socket, task = await begin(app(runner), initial=initial)
        await asyncio.wait_for(task, 1)
        assert socket.records[-1]['reason'] == 'lease_expired'
        assert len(seen) > 1
        assert {item['position'] for item in seen} == {3.25}
        assert {item['expires_at'] for item in seen} == {initial['expires_at']}
    asyncio.run(run())


def test_stopped_ack_waits_for_runner_finally_cleanup_and_copied_input_context():
    async def run():
        in_cleanup, release_cleanup, cleaned = Event(), Event(), Event()
        input_context = contextvars.ContextVar('ws_input_context', default='missing')
        token = input_context.set('modal-input-fixture')

        def runner(session, detector, reader, publish, **kwargs):
            assert input_context.get() == 'modal-input-fixture'
            try:
                publish(record(session, 'ready'))
                while reader()['active']:
                    time.sleep(.001)
                terminal = record(session, 'stopped', reason='control_stopped')
                publish(terminal)
                return terminal
            finally:
                in_cleanup.set()
                release_cleanup.wait(1)
                cleaned.set()

        try:
            socket, task = await begin(app(runner))
            await wait_until(lambda: bool(socket.records))
            socket.feed({'type': 'control', 'control': control(active=False)})
            await wait_until(in_cleanup.is_set)
            assert not any(item['state'] == 'stopped' for item in socket.records)
            assert not task.done()
            release_cleanup.set()
            await asyncio.wait_for(task, 1)
            assert cleaned.is_set() and socket.records[-1]['state'] == 'stopped'
            assert socket.outgoing[-1]['code'] == 1000
        finally:
            release_cleanup.set()
            input_context.reset(token)
    asyncio.run(run())


def test_disconnect_stops_local_control_and_does_not_emit_a_false_stopped_ack():
    async def run():
        cleaned = Event()

        def runner(session, detector, reader, publish, **kwargs):
            try:
                publish(record(session, 'ready'))
                while reader()['active']:
                    time.sleep(.001)
                return record(session, 'stopped')
            finally:
                cleaned.set()

        socket, task = await begin(app(runner))
        await wait_until(lambda: bool(socket.records))
        socket.incoming.put_nowait({'type': 'websocket.disconnect', 'code': 1001})
        await asyncio.wait_for(task, 1)
        assert cleaned.is_set()
        assert not any(item['state'] == 'stopped' for item in socket.records)
    asyncio.run(run())


def test_slow_socket_coalesces_burst_to_latest_terminal_snapshot_with_bounded_state():
    async def run():
        socket = Socket()
        socket.send_gate = asyncio.Event()
        cleaned = Event()

        def runner(session, detector, reader, publish, **kwargs):
            publish(record(session, 'running', worker_sequence=1))
            assert socket.send_entered.wait(1)
            for sequence in range(2, 1002):
                publish(record(session, 'running', worker_sequence=sequence))
            cleaned.set()
            return record(session, 'stopped', worker_sequence=1002, metrics=kwargs['state_metrics']())

        socket, task = await begin(app(runner), socket)
        await wait_until(cleaned.is_set)
        await asyncio.sleep(.01)
        socket.send_gate.set()
        await asyncio.wait_for(task, 1)
        assert len(socket.records) == 2
        assert socket.records[-1]['worker_sequence'] == 1002
        assert socket.records[-1]['state'] == 'stopped'
        assert socket.records[-1]['metrics']['max_pending'] == 1
        assert socket.records[-1]['metrics']['coalesced_snapshots'] > 0
    asyncio.run(run())


def test_nonprimitive_worker_payload_is_rejected_without_sending_arrays():
    async def run():
        def runner(session, detector, reader, publish, **kwargs):
            publish({'state': 'running', 'frames': np.zeros((75, 16, 16, 3))})
        socket, task = await begin(app(runner))
        await asyncio.wait_for(task, 1)
        assert socket.records[-1]['state'] == 'error'
        assert socket.records[-1]['reason'] == 'runner_failed'
        assert socket.records[-1]['error_type'] == 'TypeError'
        assert 'frames' not in socket.records[-1]
    asyncio.run(run())


def test_checkpoint_error_is_sanitized_and_uses_existing_public_phase_codes():
    async def run():
        def unavailable(session):
            raise OSError('https://private.invalid/token?secret=never-log')
        application = ReplayASGI(None, unavailable, token=TOKEN, phase_getter=lambda: 'load')
        socket, task = await begin(application)
        await asyncio.wait_for(task, 1)
        result = socket.records[-1]
        assert result['reason'] == 'checkpoint_unavailable'
        assert result['error_code'] == 'checkpoint_load_failed'
        assert result['error_type'] == 'OSError' and result['error_phase'] == 'load'
        assert 'never-log' not in str(result) and 'private.invalid' not in str(result)
    asyncio.run(run())


def test_asgi_lifespan_and_non_websocket_requests_do_not_start_gpu_work():
    async def run():
        application = app(lambda *a, **k: pytest.fail('Lifecycle runner'))
        socket = Socket()
        socket.incoming.put_nowait({'type': 'lifespan.startup'})
        socket.incoming.put_nowait({'type': 'lifespan.shutdown'})
        await application({'type': 'lifespan'}, socket.receive, socket.send)
        assert [item['type'] for item in socket.outgoing] == ['lifespan.startup.complete', 'lifespan.shutdown.complete']
        socket.outgoing.clear()
        await application({'type': 'http'}, socket.receive, socket.send)
        assert socket.outgoing[0]['status'] == 404
    asyncio.run(run())


def test_authoritative_zero_clock_revocation_preserves_old_epoch_position_and_lease():
    initial = control(position=5, clock_epoch=2, sources=spec()['matches'])
    state = _Control(spec(), initial)
    state.update(control(active=False, playing=False, position=0, clock_epoch=0,
                         expires_at=initial['expires_at'] + 100, sources='ignored-on-revoke'))
    value = state.read()
    assert value['position'] == 5 and value['clock_epoch'] == 2
    assert value['sources'] == initial['sources']
    assert not value['active'] and not value['playing']
    assert value['expires_at'] == initial['expires_at']


def test_zero_clock_revocation_gets_stopped_ack_after_active_playback():
    async def run():
        def runner(session, detector, reader, publish, **kwargs):
            publish(record(session, 'ready', clock_epoch=2))
            while reader()['active']:
                time.sleep(.001)
            value = reader()
            assert value['position'] == 5 and value['clock_epoch'] == 2
            return record(session, 'stopped', clock_epoch=2)

        socket, task = await begin(app(runner), initial=control(position=5, clock_epoch=2))
        await wait_until(lambda: bool(socket.records))
        socket.feed({'type': 'control', 'control': control(active=False, position=0, clock_epoch=0)})
        await asyncio.wait_for(task, 1)
        assert socket.records[-1]['state'] == 'stopped'
        assert socket.outgoing[-1]['code'] == 1000
    asyncio.run(run())


def test_bad_control_revokes_runner_and_is_rejected_without_leaking_clock_values():
    async def run():
        cleaned = Event()

        def runner(session, detector, reader, publish, **kwargs):
            try:
                publish(record(session, 'ready'))
                while reader()['active']:
                    time.sleep(.001)
                return record(session, 'stopped')
            finally:
                cleaned.set()

        socket, task = await begin(app(runner))
        await wait_until(lambda: bool(socket.records))
        socket.feed({'type': 'control', 'control': control(position=1)})
        await asyncio.wait_for(task, 1)
        assert cleaned.is_set()
        assert socket.records[-1]['reason'] == 'control_invalid'
        assert not any(item['state'] == 'stopped' for item in socket.records)
    asyncio.run(run())


def test_disconnected_stalled_runner_has_one_bounded_wait_and_keeps_gpu_session_gate():
    async def run():
        entered, release, exited = Event(), Event(), Event()

        def runner(session, detector, reader, publish, **kwargs):
            try:
                publish(record(session, 'ready'))
                entered.set()
                release.wait(1)
                return record(session, 'stopped')
            finally:
                exited.set()

        application = ReplayASGI(None, lambda session: object(), runner=runner, token=TOKEN,
            resource_factory=lambda: nullcontext(SimpleNamespace(stats={})), shutdown_timeout=.02)
        try:
            socket, task = await begin(application)
            await wait_until(entered.is_set)
            started = time.monotonic()
            socket.incoming.put_nowait({'type': 'websocket.disconnect'})
            await asyncio.wait_for(task, .3)
            assert time.monotonic() - started < .15
            assert not any(item['state'] == 'stopped' for item in socket.records)
            second = Socket()
            await application(second.scope, second.receive, second.send)
            assert second.outgoing[-1]['code'] == 1013  # No second GPU runner can overlap it.
        finally:
            release.set()
            await wait_until(exited.is_set)
    asyncio.run(run())


def test_modal_websocket_uses_the_existing_bounded_pool_with_ingress_auth_and_own_secret(monkeypatch):
    from touchline.ml_inference.streaming.contract import MAX_GPU_WORKERS
    from tests.support.ml_inference.worker import load_app

    module, registrations, _ = load_app(monkeypatch)
    limits = registrations['ReplayWorker']
    assert limits['max_containers'] == 10 * MAX_GPU_WORKERS and limits['min_containers'] == 0
    assert [secret.name for secret in limits['secrets']] == ['matchpulse-inference-replay-auth']
    assert module.ReplayWorker.web.asgi_options == {'requires_proxy_auth': True}
    assert 'secrets' not in registrations['FeatureWorker']
    assert 'secrets' not in registrations['ClassifierWorker']
