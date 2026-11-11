"""Offline tests for unit replay test_control_takeover."""


import asyncio
import copy
import pytest
from touchline.replay import ReplaySession, SourceRegistry
from tests.support.captions.session import CaptionWorker, event
from tests.support.replay.session import Clock, FakeBridge, configuration, playback, scores


def claim(session, client='observer', **values):
    return session.receive(client, {'type': 'claim_control', 'session_id': session.session_id, **values})


async def setup():
    clock, bridge, captions, published = Clock(), FakeBridge(), CaptionWorker(), []
    session = await ReplaySession(SourceRegistry.from_env({'MATCHPULSE_MATCH_COUNT': '2'}),
        config=configuration(max_duration_seconds=2400, max_estimated_cost_usd=20),
        bridge=bridge, caption_worker=captions, now=clock, monotonic=clock,
        session_id='session', publish=published.append).initialize()
    session.connect('owner')
    session.connect('observer')
    session.receive('owner', playback(session, position=5))
    await session._control_tick()
    ready = scores()
    ready.update(state='ready', scores=[])
    session.accept_snapshot(ready)
    return session, clock, bridge, captions, published


def test_takeover_preserves_owned_work_clock_source_director_and_both_immutable_allowances():
    async def scenario():
        session, _, bridge, captions, published = await setup()
        try:
            session.receive('owner', {'type': 'director_mode', 'session_id': 'session', 'mode': 'relaxed'})
            session.receive('owner', {'type': 'select', 'session_id': 'session', 'match_id': 'cam-2'})
            session.receive('owner', playback(session, match_id='cam-2', position=5, rate=1.25))
            before = session.snapshot()
            owned = (session.director, session._handle, session._monitor_started_at,
                     session._reserved_compute_usd, session._caption_deadline, session.last_report_at)
            calls, caption_calls, generation = len(bridge.calls), len(captions.calls), session._caption_generation

            assert claim(session) is None
            assert session.controller == 'observer'
            assert session.snapshot('observer')['controls_enabled']
            assert not session.snapshot('owner')['controls_enabled']
            assert session.snapshot()['can_claim_control'] is True
            after = session.snapshot()
            for key in ('session_id', 'model_version', 'mode', 'state', 'playback', 'current_match_id',
                        'manual_hold_until', 'auto_requested', 'matches'):
                assert after[key] == before[key]
            for key in ('mode', 'config', 'history', 'last_switch_at', 'auto_cooldown_until'):
                assert after['director'][key] == before['director'][key]
            assert (session.director, session._handle, session._monitor_started_at,
                    session._reserved_compute_usd, session._caption_deadline, session.last_report_at) == owned
            assert session.caption_worker is captions and session._worker_ready
            assert session._caption_generation == generation + 1
            assert captions.calls[caption_calls:] == [('stop', 'control_transferred')]
            assert not session._stop_requested and len(bridge.calls) == calls
            assert session._caption_actual_match_id == 'cam-2'
            assert published[-1]['type'] == 'session'

            session.disconnect('owner')
            assert session.controller == 'observer' and not session._stop_requested
            session.receive('observer', playback(session, match_id='cam-2', position=5, rate=1.25))
            await session._control_tick()
            assert session._handle == owned[1] and session._caption_deadline == owned[4]
            assert len([call for call in bridge.calls if call[0] == 'start']) == 1
            assert not [call for call in bridge.calls if call[0] == 'stop']
        finally:
            await session.close()

    asyncio.run(scenario())


def test_handoff_uses_only_original_lease_until_fresh_valid_new_owner_report():
    async def scenario():
        session, clock, bridge, captions, _ = await setup()
        try:
            original = copy.deepcopy(bridge.calls[0][1])
            original_expiry = original['expires_at']
            clock.value += 2
            claim(session)
            count, caption_count = len(bridge.calls), len(captions.updates)
            assert session.control()['active'] is False
            assert session._caption_view() is None
            assert session.receive('observer', playback(session, position=float('nan')))['code'] == 'invalid_playback'
            assert session._controller_report_pending
            await session._control_tick()
            session._update_captions()
            assert len(bridge.calls) == count and len(captions.updates) == caption_count
            assert bridge.calls[0][1]['expires_at'] == original_expiry
            assert session.position == original['position'] == 5

            clock.value += .25
            assert session.receive('observer', playback(session, position=5.25)) is None
            assert not session._controller_report_pending
            await session._control_tick()
            control = bridge.calls[-1][1]
            assert bridge.calls[-1][0] == 'control'
            assert control['active'] and control['position'] == 5.25
            assert control['expires_at'] == clock.value + session.config.heartbeat_seconds
            assert captions.updates[-1]['position'] == 5.25
            assert len([call for call in bridge.calls if call[0] == 'start']) == 1
        finally:
            await session.close()

    asyncio.run(scenario())


def test_transfer_invalidates_pending_ack_confirmation_and_captions_and_denies_old_owner_writes():
    async def scenario():
        session, _, bridge, captions, published = await setup()
        try:
            assert session.accept_snapshot(scores(sequence=2))
            recommendation = session._last_recommendation.copy()
            assert recommendation['selected_match_id'] == 'cam-2'
            session.receive('owner', playback(session, position=5))
            # A previously emitted partial is tied to the original generation.
            session._caption_pending_match_id = session._caption_pending_until = None
            session._update_captions()
            old_caption = event(session)
            assert session._accept_caption_event(old_caption)
            session._last_ack_sequence = recommendation['sequence']
            claim(session)
            assert session._last_recommendation is None and session._last_ack_sequence is None
            assert session.director.challenger_wins == 0 and session.director._proposal is None
            assert not session._accept_caption_event(old_caption)
            assert not session.accept_snapshot(scores(sequence=3))
            assert session._last_recommendation is None
            assert not session._evaluate_director()
            ack = {'type': 'ack', 'session_id': 'session', 'sequence': recommendation['sequence'],
                   'clock_epoch': session.clock_epoch, 'match_id': 'cam-2'}
            messages = [playback(session, position=100),
                {'type': 'select', 'session_id': 'session', 'match_id': 'cam-2'},
                {'type': 'auto', 'session_id': 'session', 'enabled': False},
                {'type': 'captions', 'session_id': 'session', 'enabled': False},
                {'type': 'director_mode', 'session_id': 'session', 'mode': 'balanced'},
                {'type': 'source_preparing', 'session_id': 'session', 'match_id': 'cam-2'}, ack]
            state, calls, caption_count = session.snapshot(), len(bridge.calls), len(captions.calls)
            for message in messages:
                assert session.receive('owner', message)['code'] == 'viewer_in_use'
            assert session.receive('observer', ack)['code'] == 'stale_ack'
            assert session.position == 5 and session.director.current_match_id == 'cam-1'
            assert session.snapshot()['director'] == state['director']
            assert 'last_ack' not in session.metrics
            assert len(bridge.calls) == calls and len(captions.calls) == caption_count
            assert published[-1]['type'] == 'session'
        finally:
            await session.close()

    asyncio.run(scenario())


def test_invalid_claimants_and_sessions_cannot_transfer_and_owner_retry_only_republishes_roles():
    async def scenario():
        session, _, bridge, captions, published = await setup()
        try:
            session.connect('departed')
            session.disconnect('departed')
            for client in ('unknown', 'departed'):
                assert claim(session, client)['code'] == 'viewer_not_registered'
            assert claim(session, session_id='expired-session')['code'] == 'session_mismatch'
            assert session.controller == 'owner'
            before = (session._caption_generation, session.last_report_at, len(captions.calls), len(bridge.calls))
            count = len(published)
            assert claim(session, 'owner') is None
            assert len(published) == count + 1
            assert (session._caption_generation, session.last_report_at, len(captions.calls), len(bridge.calls)) == before
            assert not session._controller_report_pending
            session._closing = True
            assert claim(session)['code'] == 'session_closed'
            assert session.controller == 'owner'
            session._closing = False
        finally:
            await session.close()
        assert claim(session)['code'] == 'session_closed'

    asyncio.run(scenario())


def test_repeated_claims_cannot_renew_original_worker_lease_without_actual_playback():
    async def scenario():
        session, clock, bridge, _, _ = await setup()
        try:
            original_report, handle = session.last_report_at, session._handle
            claim(session)
            clock.value += 4
            claim(session, 'owner')
            claim(session, 'owner')
            await session._control_tick()
            assert session.last_report_at == original_report and session._handle == handle
            clock.value += 1
            await session._control_tick()
            assert session._handle is None and session.state == 'waiting'
            assert len([call for call in bridge.calls if call[0] == 'start']) == 1
            assert len([call for call in bridge.calls if call[0] == 'stop']) == 1
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('reason,status', [('viewer_disabled', 'disabled'), ('budget_deadline', 'stopped'),
                                           ('auth_error', 'unavailable')])
def test_transfer_preserves_manual_off_and_terminal_caption_reasons(reason, status):
    async def scenario():
        session, _, _, _, _ = await setup()
        try:
            session._caption_enabled = False
            session._caption_user_enabled = reason != 'viewer_disabled'
            session._caption_revoke(reason, status=status)
            deadline = session._caption_deadline
            claim(session)
            state = session.caption_state()
            assert state['status'] == status and state['reason'] == reason
            assert state['deadline'] == deadline and state['enabled'] is False
            assert state['user_enabled'] is (reason != 'viewer_disabled')
        finally:
            await session.close()

    asyncio.run(scenario())
