"""Offline tests for integration api test_control_takeover."""


import asyncio
import copy
import json
import websockets
from touchline.replay import ReplaySession, SourceRegistry
from tests.support.replay.session import Clock, FakeBridge, configuration, playback, scores
from touchline.api.websocket import WebSocketManager


class SingleCaptionWorker:
    """Model one live generation so a transfer cannot overlap caption leases."""

    def __init__(self):
        self.calls = []
        self.active = None
        self.maximum_active = 0

    def update(self, view):
        token = (view['session_id'], view['match_id'], view['generation'], view['clock_epoch'])
        assert self.active in (None, token), 'Old caption generation was not revoked'
        self.active = token
        self.maximum_active = max(self.maximum_active, 1)
        self.calls.append(('update', copy.deepcopy(view)))

    def stop(self, reason):
        self.active = None
        self.calls.append(('stop', reason))

    def close(self):
        self.active = None
        self.calls.append(('close',))
        return True

    @property
    def updates(self):
        return [call[1] for call in self.calls if call[0] == 'update']


async def receive_matching(socket, predicate):
    async def receive():
        for _ in range(32):
            message = json.loads(await socket.recv())
            if predicate(message):
                return message
        raise AssertionError('Expected bounded protocol response was not delivered')

    return await asyncio.wait_for(receive(), 2)


async def settled(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(.001)
    await asyncio.wait_for(wait(), 2)


async def send(socket, message):
    await socket.send(json.dumps(message))


async def fixture():
    clock, bridge, captions = Clock(), FakeBridge(), SingleCaptionWorker()
    session = await ReplaySession(
        SourceRegistry.from_env({'MATCHPULSE_MATCH_COUNT': '2'}), config=configuration(),
        bridge=bridge, caption_worker=captions, caption_max_audio_seconds=28800,
        caption_max_cost_usd=3.60, now=clock, monotonic=clock, session_id='session').initialize()
    manager = WebSocketManager(session, host='127.0.0.1', port=0)
    await manager.start()
    port = manager.server.sockets[0].getsockname()[1]
    return session, manager, bridge, captions, clock, f'ws://127.0.0.1:{port}'


def test_takeover_broadcasts_roles_revokes_old_writes_and_preserves_owned_work():
    async def scenario():
        session, manager, bridge, captions, clock, url = await fixture()
        old, new = None, None
        try:
            old = await websockets.connect(url)
            initial = await receive_matching(old, lambda item: item['type'] == 'session')
            assert initial['controls_enabled']
            new = await websockets.connect(url)
            observer = await receive_matching(new, lambda item: item['type'] == 'session')
            assert not observer['controls_enabled']
            assert initial['can_claim_control'] and observer['can_claim_control']

            await send(old, playback(session, position=5, available=True))
            await settled(lambda: session.position == 5 and bool(captions.updates))
            await session._control_tick()
            assert session._handle == 'own-handle'
            assert session.accept_snapshot(scores())
            proposal = await receive_matching(old, lambda item: item['type'] == 'recommendation')
            assert proposal['selected_match_id'] == 'cam-2'
            original = {
                'session_id': session.session_id, 'model': session.config.model_version,
                'config': session.config, 'handle': session._handle, 'epoch': session.clock_epoch,
                'reserved': session._reserved_compute_usd, 'started': session._monitor_started_at,
                'caption_deadline': session.caption_state()['deadline'],
                'caption_max_seconds': session.caption_max_audio_seconds,
                'caption_budget': session.caption_max_cost_usd,
                'expiry': session.control()['expires_at'],
            }
            old_generation = session.caption_state()['generation']
            old_updates = len(captions.updates)

            clock.value += 1
            await send(new, {'type': 'claim_control', 'session_id': 'session'})
            claimed = await receive_matching(new, lambda item: item['type'] == 'session'
                                             and item['controls_enabled'])
            displaced = await receive_matching(old, lambda item: item['type'] == 'session'
                                               and not item['controls_enabled'])
            assert claimed['session_id'] == displaced['session_id'] == original['session_id']
            assert claimed['playback']['position'] == 5
            assert session._last_recommendation is None and session._last_ack_sequence is None
            assert session.caption_state()['generation'] == old_generation + 1
            assert captions.calls[-1] == ('stop', 'control_transferred')
            assert captions.active is None and len(captions.updates) == old_updates
            assert not session.control()['active']
            assert session.control()['expires_at'] <= original['expiry']
            control_count = sum(call[0] == 'control' for call in bridge.calls)
            session._update_captions()
            assert captions.active is None and len(captions.updates) == old_updates
            await session._control_tick()  # The original unextended lease bridges the handoff.
            assert session._handle == original['handle'] and not session._stop_requested
            assert sum(call[0] == 'control' for call in bridge.calls) == control_count

            attempted = [
                playback(session, position=999, available=True),
                {'type': 'select', 'session_id': 'session', 'match_id': 'cam-2'},
                {'type': 'auto', 'session_id': 'session', 'enabled': False},
                {'type': 'captions', 'session_id': 'session', 'enabled': False},
                {'type': 'source_preparing', 'session_id': 'session', 'match_id': 'cam-2'},
                {'type': 'director_mode', 'session_id': 'session', 'mode': 'relaxed'},
                {'type': 'ack', 'session_id': 'session', 'sequence': proposal['sequence'],
                 'clock_epoch': original['epoch'], 'match_id': 'cam-2'},
            ]
            for message in attempted:
                await send(old, message)
                denied = await receive_matching(old, lambda item: item['type'] == 'error')
                assert denied['code'] == 'viewer_in_use'
            assert session.position == 5 and session.director.current_match_id == 'cam-1'
            assert session.director.auto_enabled and session.director.director_mode == 'all_action'
            assert session.caption_state()['enabled'] and 'last_ack' not in session.metrics

            await send(new, playback(session, position=6, available=True))
            await settled(lambda: session.position == 6 and len(captions.updates) > old_updates)
            latest = captions.updates[-1]
            assert latest['position'] == 6 and latest['report_received_at'] == clock.value
            assert latest['expires_at'] == clock.value + 5
            assert latest['generation'] == session.caption_state()['generation']
            assert latest['deadline'] == original['caption_deadline']
            assert captions.maximum_active == 1

            await old.close()
            old = None
            await settled(lambda: len(manager.clients) == 1)
            await session._control_tick()
            assert session._handle == original['handle'] and not session._stop_requested
            assert session.playing and session.control()['active']
            assert session.control()['position'] == 6 and session.control()['expires_at'] == clock.value + 5
            assert session.session_id == original['session_id']
            assert session.config is original['config'] and session.config.model_version == original['model']
            assert session.clock_epoch == original['epoch']
            assert session._reserved_compute_usd == original['reserved']
            assert session._monitor_started_at == original['started']
            assert session.caption_state()['deadline'] == original['caption_deadline']
            assert session.caption_max_audio_seconds == original['caption_max_seconds']
            assert session.caption_max_cost_usd == original['caption_budget']
            assert len([call for call in bridge.calls if call[0] == 'start']) == 1
            assert not any(call[0] == 'stop' for call in bridge.calls)
        finally:
            if old is not None:
                await old.close()
            if new is not None:
                await new.close()
            await manager.close()
            await session.close()
    asyncio.run(scenario())


def test_current_owner_claims_are_idempotent_and_wrong_session_cannot_take_control():
    async def scenario():
        session, manager, _bridge, captions, clock, url = await fixture()
        try:
            async with websockets.connect(url) as old, websockets.connect(url) as new:
                await receive_matching(old, lambda item: item['type'] == 'session')
                observer = await receive_matching(new, lambda item: item['type'] == 'session')
                assert not observer['controls_enabled']
                await send(old, playback(session, position=5, available=True))
                await settled(lambda: bool(captions.updates))
                deadline = session.caption_state()['deadline']

                await send(new, {'type': 'claim_control', 'session_id': 'another-session'})
                denied = await receive_matching(new, lambda item: item['type'] == 'error')
                assert denied['code'] == 'session_mismatch'
                assert session.controls_enabled(session.clients[0]) and len(captions.updates) == 1

                await send(new, {'type': 'claim_control', 'session_id': 'session'})
                await receive_matching(new, lambda item: item['type'] == 'session' and item['controls_enabled'])
                await send(new, playback(session, position=5, available=True))
                await settled(lambda: len(captions.updates) == 2)
                before = (session.sequence, session._caption_generation, len(captions.calls), session.last_report_at)
                clock.value += .5
                for _ in range(32):
                    await send(new, {'type': 'claim_control', 'session_id': 'session'})
                # Ordered invalid input is a processing barrier after all claims.
                await send(new, {'type': 'claim_control', 'session_id': 'wrong'})
                denied = await receive_matching(new, lambda item: item['type'] == 'error'
                                                and item['code'] == 'session_mismatch')
                assert denied['sequence'] > before[0]
                assert (session._caption_generation, len(captions.calls), session.last_report_at) == before[1:]
                assert session.caption_state()['deadline'] == deadline
                assert session.control()['expires_at'] == 1005
                assert captions.maximum_active == 1
                assert sum(call == ('stop', 'control_transferred') for call in captions.calls) == 1
                assert all(len(connection.pending) <= 4 for connection in manager.clients.values())
        finally:
            await manager.close()
            await session.close()
    asyncio.run(scenario())


def test_takeover_without_new_playback_cannot_extend_the_original_worker_lease():
    async def scenario():
        session, manager, bridge, captions, clock, url = await fixture()
        try:
            async with websockets.connect(url) as old, websockets.connect(url) as new:
                await receive_matching(old, lambda item: item['type'] == 'session')
                await receive_matching(new, lambda item: item['type'] == 'session')
                await send(old, playback(session, position=5, available=True))
                await settled(lambda: session.position == 5 and bool(captions.updates))
                await session._control_tick()
                assert session._handle == 'own-handle' and captions.active is not None
                expiry = session.control()['expires_at']
                deadline, reserved = session.caption_state()['deadline'], session._reserved_compute_usd
                updates = len(captions.updates)
                clock.value += 1
                await send(new, {'type': 'claim_control', 'session_id': 'session'})
                await receive_matching(new, lambda item: item['type'] == 'session' and item['controls_enabled'])
                assert captions.active is None

                clock.value = expiry + .01
                await session._control_tick()
                session._update_captions()
                await session._control_tick()

                assert session._handle is None and not session.control()['active']
                assert session.position == 5 and len(captions.updates) == updates
                assert session.caption_state()['deadline'] == deadline
                assert session._reserved_compute_usd == reserved
                assert len([call for call in bridge.calls if call[0] == 'start']) == 1
                assert len([call for call in bridge.calls if call[0] == 'stop']) == 1
                assert captions.maximum_active == 1
        finally:
            await manager.close()
            await session.close()
    asyncio.run(scenario())
