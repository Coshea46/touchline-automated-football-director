"""Offline tests for integration ml_inference streaming test_ws_client."""


import asyncio
import json
import time
from http import HTTPStatus
from threading import Event, Thread
import pytest
import websockets
from touchline.ml_inference.streaming.ws_client import ReplayWebSocketClient
from tests.support.ml_inference.client import multi_session, running_shard, session


TOKEN = 'fixture-proxy-token.not-a-secret'


def control(**extra):
    return {'position': 0, 'playing': False, 'active': True, 'expires_at': time.time() + 5,
            'clock_epoch': 0, **extra}


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError('Local replay condition did not complete')


class Server:
    def __init__(self, handler, *, process_request=None):
        self.handler, self.process_request = handler, process_request
        self.ready = Event()
        self.error = None
        self.loop = asyncio.new_event_loop()
        self.thread = Thread(target=self.run, daemon=True, name='test-replay-server')
        self.thread.start()
        assert self.ready.wait(2)
        if self.error:
            raise self.error

    def run(self):
        asyncio.set_event_loop(self.loop)

        async def start():
            self.server = await websockets.serve(self.handler, '127.0.0.1', 0,
                                                  process_request=self.process_request, max_size=512 * 1024)
            self.port = self.server.sockets[0].getsockname()[1]
        try:
            self.loop.run_until_complete(start())
        except Exception as exc:  # noqa: BLE001 - relay local fixture startup errors to the test thread.
            self.error = exc
        self.ready.set()
        if self.error is None:
            self.loop.run_forever()
        self.loop.close()

    @property
    def url(self):
        return f'ws://127.0.0.1:{self.port}/session'

    def close(self):
        async def stop():
            self.server.close()
            await self.server.wait_closed()
        asyncio.run_coroutine_threadsafe(stop(), self.loop).result(timeout=3)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=1)
        assert not self.thread.is_alive()


@pytest.fixture
def servers():
    created = []

    def create(*args, **kwargs):
        server = Server(*args, **kwargs)
        created.append(server)
        return server

    yield create
    for server in created:
        server.close()


def client_for(server, **kwargs):
    return ReplayWebSocketClient(server.url, TOKEN, allow_loopback=True,
                                  control_interval=.05, stop_timeout=.75, **kwargs)


def snapshot(spec, sequence=1, state='running', epoch=0):
    result = running_shard(spec, sequence=sequence, state=state, epoch=epoch)
    result['produced_at'] = time.time()
    for record in result['scores']:
        record['produced_at'] = result['produced_at']
    return result


def test_real_two_shard_sockets_authenticate_merge_and_stop_without_sdk(servers, monkeypatch):
    received, headers, updates = [], [], []

    async def handler(socket):
        headers.append(dict(socket.request_headers))
        start = json.loads(await socket.recv())
        assert start['type'] == 'start'
        spec = start['spec']
        first = json.loads(await socket.recv())
        received.append((spec, first['control']))
        await socket.send(json.dumps(snapshot(spec)))
        async for raw in socket:
            message = json.loads(raw)
            if not message['control']['active']:
                await socket.send(json.dumps(snapshot(spec, 2, 'stopped')))
                return
            updates.append((spec, message['control']))

    server = servers(handler)
    client = client_for(server)
    monkeypatch.setattr('touchline.ml_inference.streaming.client.importlib.import_module',
                        lambda _: (_ for _ in ()).throw(AssertionError('No SDK import')))
    spec = multi_session(gpu_workers=2)
    client.update_control('owned-session', control(position=5, sources=spec['matches']))
    owned = client.start(spec)
    merged = wait_for(lambda: client.latest('owned-session'))
    assert len(merged['scores']) == 7 and len(received) == 2
    assert sorted(len(spec['matches']) for spec, _ in received) == [3, 4]
    assert all({source['match_id'] for source in value['sources']} == {source['match_id'] for source in spec['matches']}
               for spec, value in received)
    assert all(header['authorization'] == 'Bearer ' + TOKEN for header in headers)
    assert all(header['x-matchpulse-replay-token'] == TOKEN for header in headers)
    assert client.latest('owned-session')['worker_sequence'] == merged['worker_sequence']
    refreshed = [{**source, 'source_url': source['source_url'] + '?refresh=yes'} for source in spec['matches']]
    client.update_control('owned-session', control(position=6, sources=refreshed))
    wait_for(lambda: len(updates) >= 2)
    assert all({source['match_id'] for source in value['sources']} == {source['match_id'] for source in shard['matches']}
               for shard, value in updates)
    assert all(source['source_url'].endswith('?refresh=yes') for _, value in updates for source in value['sources'])
    assert client.stop('owned-session', owned) == {'confirmed': True, 'method': 'websocket_graceful'}
    assert all(handle._done.is_set() and handle._thread.daemon for handle in owned.handles)
    assert not client._local._values


def test_cold_handshake_sends_freshest_control_and_coalesces_without_extrapolation(servers):
    controls = []

    async def delayed_handshake(*_):
        await asyncio.sleep(.2)

    async def handler(socket):
        spec = json.loads(await socket.recv())['spec']
        async for raw in socket:
            value = json.loads(raw)['control']
            controls.append(value)
            if not value['active']:
                await socket.send(json.dumps(snapshot(spec, 2, 'stopped')))
                return
            if len(controls) == 1:
                await socket.send(json.dumps(snapshot(spec)))

    server = servers(handler, process_request=delayed_handshake)
    client = client_for(server)
    client.update_control('owned-session', control(position=0))
    before = time.monotonic()
    owned = client.start(session())
    assert time.monotonic() - before < .15
    expiry = time.time() + 4.5
    for position in range(1, 41):
        client.update_control('owned-session', control(position=position, expires_at=expiry))
    wait_for(lambda: controls)
    assert controls[0]['position'] == 40 and controls[0]['expires_at'] == expiry
    time.sleep(.15)
    assert len(controls) == 1  # No invented heartbeat/position or growing command queue.
    assert len(client._local._values) <= 2
    client.stop('owned-session', owned)


def test_expired_control_is_not_renewed_by_the_transport(servers):
    controls = []

    async def delay(*_):
        await asyncio.sleep(.15)

    async def handler(socket):
        spec = json.loads(await socket.recv())['spec']
        value = json.loads(await socket.recv())['control']
        controls.append(value)
        await socket.send(json.dumps(snapshot(spec, state='stopped')))

    client = client_for(servers(handler, process_request=delay))
    expiry = time.time() + .05
    client.update_control('owned-session', control(expires_at=expiry))
    owned = client.start(session())
    wait_for(lambda: controls)
    assert controls[0]['expires_at'] == expiry and controls[0]['expires_at'] < time.time()
    client.stop('owned-session', owned)


def test_auth_failure_publishes_only_sanitized_error_and_stop_is_unconfirmed(servers):
    async def reject(*_):
        return HTTPStatus.UNAUTHORIZED, [], b'Unauthorized'

    server = servers(lambda _: None, process_request=reject)
    client = client_for(server)
    client.update_control('owned-session', control())
    owned = client.start(session())
    result = wait_for(lambda: client.latest('owned-session'))
    assert result['state'] == 'error' and result['reason'] == 'processing_error'
    assert TOKEN not in json.dumps(result) and server.url not in json.dumps(result)
    with pytest.raises(RuntimeError, match='TimeoutError'):
        client.stop('owned-session', owned)
    assert not client._local._values


def test_network_loss_does_not_claim_gpu_cleanup_confirmation(servers):
    async def handler(socket):
        spec = json.loads(await socket.recv())['spec']
        await socket.recv()
        await socket.send(json.dumps(snapshot(spec)))
        socket.transport.close()

    client = client_for(servers(handler))
    client.update_control('owned-session', control())
    owned = client.start(session())
    result = wait_for(lambda: (value if (value := client.latest('owned-session')) and value['state'] == 'error' else None))
    assert result['error_type'] == 'ConnectionClosedError'
    with pytest.raises(RuntimeError, match='TimeoutError'):
        client.stop('owned-session', owned)
    assert not client._local._values


def test_seek_discards_inflight_old_epoch_without_dropping_persistent_connection(servers):
    starts, progressed = [], Event()

    async def handler(socket):
        spec = json.loads(await socket.recv())['spec']
        starts.append(spec)
        await socket.recv()
        await socket.send(json.dumps(snapshot(spec)))
        async for raw in socket:
            value = json.loads(raw)['control']
            if not value['active']:
                # Final cleanup may report the last processed epoch, while the
                # viewer already issued a second seek immediately before stop.
                await socket.send(json.dumps(snapshot(spec, 4, 'stopped', epoch=1)))
                return
            if value['clock_epoch'] == 1:
                await socket.send(json.dumps(snapshot(spec, 2, epoch=0)))
                await socket.send(json.dumps(snapshot(spec, 3, epoch=1)))
                progressed.set()

    client = client_for(servers(handler))
    client.update_control('owned-session', control())
    owned = client.start(session())
    wait_for(lambda: client.latest('owned-session'))
    client.update_control('owned-session', control(position=1, clock_epoch=1))
    assert progressed.wait(2)
    updated = wait_for(lambda: (value if (value := client.latest('owned-session')) and value['clock_epoch'] == 1 else None))
    assert updated['state'] == 'running' and len(starts) == 1
    client.update_control('owned-session', control(position=0, clock_epoch=2))
    assert client.stop('owned-session', owned)['confirmed']


def test_pending_cold_connection_can_be_cancelled_without_a_non_daemon_waiter(servers):
    async def delayed(*_):
        await asyncio.sleep(.3)

    server = servers(lambda _: None, process_request=delayed)
    client = ReplayWebSocketClient(server.url, TOKEN, allow_loopback=True, stop_timeout=.1)
    client.update_control('owned-session', control())
    owned = client.start(session())
    before = time.monotonic()
    with pytest.raises(RuntimeError, match='TimeoutError'):
        client.stop('owned-session', owned)
    assert time.monotonic() - before < .75
    assert all(handle._done.wait(.5) for handle in owned.handles)
    assert not client._local._values


def test_idle_read_timeout_is_sanitized_and_cannot_claim_stopped_ack(servers):
    async def handler(socket):
        await socket.recv()
        await socket.recv()
        await socket.wait_closed()

    client = client_for(servers(handler), read_timeout=.05)
    client.update_control('owned-session', control())
    owned = client.start(session())
    error = wait_for(lambda: client.latest('owned-session'))
    assert error['state'] == 'error' and error['error_type'] == 'TimeoutError'
    with pytest.raises(RuntimeError, match='TimeoutError'):
        client.stop('owned-session', owned)


def test_missing_stopped_ack_has_bounded_shutdown_and_preserves_honest_failure(servers):
    async def handler(socket):
        spec = json.loads(await socket.recv())['spec']
        await socket.recv()
        await socket.send(json.dumps(snapshot(spec)))
        await socket.wait_closed()

    server = servers(handler)
    client = ReplayWebSocketClient(server.url, TOKEN, allow_loopback=True, stop_timeout=.1)
    client.update_control('owned-session', control())
    owned = client.start(session())
    wait_for(lambda: client.latest('owned-session'))
    before = time.monotonic()
    with pytest.raises(RuntimeError, match='TimeoutError'):
        client.stop('owned-session', owned)
    assert time.monotonic() - before < .75 and not client._local._values


def test_real_client_and_asgi_endpoint_share_source_clock_ready_and_cleanup_contract(servers):
    from touchline.ml_inference.streaming.asgi import ReplayASGI

    cleaned = []

    class Resources:
        def __init__(self):
            self.stats = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def runner(spec, _detector, read_control, publish, **_):
        publish(snapshot(spec, state='ready'))
        predicted = False
        try:
            deadline = time.monotonic() + 3
            while read_control()['active'] and time.monotonic() < deadline:
                clock = read_control()
                assert clock['position'] <= 5
                if clock['position'] == 5 and not predicted:
                    publish(snapshot(spec, 2))
                    predicted = True
                time.sleep(.005)
            return snapshot(spec, 3, 'stopped')
        finally:
            cleaned.append(spec['shard_id'])

    async def handler(socket):
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {'type': 'websocket.connect'}
            try:
                return {'type': 'websocket.receive', 'text': await socket.recv()}
            except websockets.ConnectionClosed as exc:
                return {'type': 'websocket.disconnect', 'code': exc.code}

        async def send(event):
            if event['type'] == 'websocket.send':
                await socket.send(event['text'])
            elif event['type'] == 'websocket.close':
                await socket.close(code=event['code'])

        app = ReplayASGI(object(), lambda _: object(), token=TOKEN, runner=runner,
                         resource_factory=Resources, shutdown_timeout=.5)
        await app({'type': 'websocket', 'path': '/session',
                   'headers': [(key.lower().encode(), value.encode()) for key, value in socket.request_headers.raw_items()]},
                  receive, send)

    client = client_for(servers(handler))
    spec = multi_session()
    client.update_control('owned-session', control(sources=spec['matches']))
    owned = client.start(spec)
    ready = wait_for(lambda: (value if (value := client.latest('owned-session')) and value['state'] == 'ready' else None))
    assert len(ready['health']) == 7
    assert len(client._local._values) == 5
    client.update_control('owned-session', control(position=5, playing=True, sources=spec['matches']))
    scored = wait_for(lambda: (value if (value := client.latest('owned-session')) and len(value['scores']) == 7 else None))
    assert {row['observation_end'] for row in scored['scores']} == {5}
    assert client.stop('owned-session', owned)['confirmed']
    assert sorted(cleaned) == ['0', '1', '2', '3']
    assert len(owned.handles) == 4
    assert max(len(handle._spec['matches']) for handle in owned.handles) == 2


@pytest.mark.parametrize('url', ['ws://example.com/session', 'wss://example.com/session',
                                'https://evilmodal.run/session', 'wss://user:pass@a.modal.run/session',
                                'wss://a.modal.run/session?token=secret'])
def test_only_modal_tls_or_explicit_test_loopback_endpoints_are_allowed(url):
    with pytest.raises(ValueError) as error:
        ReplayWebSocketClient(url, TOKEN)
    assert url not in str(error.value)


def test_env_requires_private_token_and_normalizes_https_base(monkeypatch):
    monkeypatch.setenv('MATCHPULSE_MODAL_WS_URL', 'https://inference.example.modal.run')
    monkeypatch.delenv('MATCHPULSE_MODAL_WS_TOKEN', raising=False)
    with pytest.raises(ValueError, match='token'):
        ReplayWebSocketClient.from_env()
    monkeypatch.setenv('MATCHPULSE_MODAL_WS_TOKEN', TOKEN)
    client = ReplayWebSocketClient.from_env()
    assert client._url == 'wss://inference.example.modal.run/session'
    assert TOKEN not in repr(client)
