"""Offline tests for unit replay test_auto_activation."""


import asyncio
import threading
import pytest
from touchline.ml_inference.models.release import NEW_MODEL_VERSION
from touchline.replay import ReplaySession, SourceRegistry
from touchline.replay.config import SessionConfig
from tests.support.captions.session import CaptionWorker
from tests.support.replay.session import Clock, FakeBridge, playback


async def setup(*, bridge=None, factory=None, seconds=120, budget=1):
    clock, captions = Clock(), CaptionWorker()
    prepared = SessionConfig(monitor=True, mode='real', model_version=NEW_MODEL_VERSION,
        checkpoint=NEW_MODEL_VERSION, max_duration_seconds=seconds, max_estimated_cost_usd=budget,
        max_restarts=0, gpu_workers=4)
    registry = SourceRegistry.from_env({'S3_BUCKET_NAME': 'match-switcher-poc-videos', 'MATCHPULSE_SOURCE_ACCESS': 'public', 'MATCHPULSE_MATCH_COUNT': '7',
        'MATCHPULSE_OFFSETS_JSON': '{"cam-1":10.5,"cam-3":31.25}'})
    session = await ReplaySession(registry, config=SessionConfig(), prepared_config=prepared,
        bridge=bridge, bridge_factory=factory, caption_worker=captions,
        now=clock, monotonic=clock, session_id='session').initialize()
    session.connect('owner')
    session.receive('owner', playback(session, position=123.4))
    return session, clock, captions


def auto(session, enabled=True, owner='owner', session_id='session'):
    return session.receive(owner, {'type': 'auto', 'session_id': session_id, 'enabled': enabled})


def ready(session, **changes):
    return {'session_id': session.session_id, 'model_version': NEW_MODEL_VERSION,
        'worker_sequence': 1, 'clock_epoch': session.clock_epoch, 'score_kind': 'model',
        'state': 'ready', 'scores': [], 'health': {f'cam-{i}': 'healthy' for i in range(1, 8)}, **changes}


def test_real_activation_queues_off_handler_and_preserves_session_director_clock_captions_and_sources():
    async def scenario():
        bridge, factory_threads = FakeBridge(), []
        session, _, captions = await setup(factory=lambda: factory_threads.append(threading.get_ident()) or bridge)
        try:
            session.director.set_mode('all_action')
            session.receive('owner', {'type': 'select', 'session_id': 'session', 'match_id': 'cam-3'})
            session.receive('owner', playback(session, match_id='cam-3', position=123.5, playing=False))
            original = (session.director, session.clock_epoch, session.position,
                        session.director.manual_hold_until, list(session.director.history), session._caption_deadline)
            initial = session.snapshot()
            assert initial['auto_can_start'] and not initial['auto_requested'] and not initial['auto_available']
            assert auto(session) is None
            assert auto(session) is None
            assert not factory_threads and not bridge.calls  # The socket handler only queues intent.
            assert session.snapshot()['auto_starting'] and not session.snapshot()['auto_can_start']
            await session._control_tick()
            assert factory_threads and factory_threads[0] != threading.get_ident()
            starts = [call for call in bridge.calls if call[0] == 'start']
            assert len(starts) == 1
            spec = starts[0][1]
            assert spec['session_id'] == 'session' and spec['start_position'] == 123.5
            assert spec['model_version'] == spec['checkpoint'] == NEW_MODEL_VERSION
            assert spec['mode'] == 'real' and len(spec['matches']) == 7
            assert spec['matches'][2]['offset_seconds'] == 31.25
            assert session.director is original[0] and session.clock_epoch == original[1]
            assert session.position == original[2] and session.director.manual_hold_until == original[3]
            assert list(session.director.history) == original[4] and session._caption_deadline == original[5]
            assert session.caption_worker is captions and session.director.director_mode == 'all_action'
            assert not session.snapshot()['auto_enabled']
            assert not session.accept_snapshot(ready(session, model_version='wrong-model'))
            session.accept_snapshot(ready(session))
            assert session.snapshot()['auto_enabled'] and not session.snapshot()['auto_starting']
            assert auto(session) is None
            await session._control_tick()
            assert len([call for call in bridge.calls if call[0] == 'start']) == 1
        finally:
            await session.close()

    asyncio.run(scenario())


def test_pending_off_cancels_before_any_factory_or_paid_start_and_observers_cannot_activate():
    async def scenario():
        calls = []
        session, _, _ = await setup(factory=lambda: calls.append(True) or FakeBridge())
        session.connect('observer')
        try:
            assert auto(session, owner='observer')['code'] == 'viewer_in_use'
            assert auto(session, session_id='old-session')['code'] == 'session_mismatch'
            assert not session._activation_requested and not calls
            assert auto(session) is None and auto(session, False) is None
            await session._control_tick()
            assert not calls and session.bridge is None and not session.config.monitor
            assert not session.snapshot()['auto_requested'] and session.snapshot()['auto_can_start']
        finally:
            await session.close()

    asyncio.run(scenario())


def test_off_during_lazy_preparation_cancels_submission_and_off_after_submission_keeps_monitoring():
    async def scenario():
        entered, release, bridge = threading.Event(), threading.Event(), FakeBridge()

        def factory():
            entered.set()
            assert release.wait(2)
            return bridge

        session, _, _ = await setup(factory=factory)
        try:
            auto(session)
            task = asyncio.create_task(session._control_tick())
            while not entered.is_set():
                await asyncio.sleep(.001)
            auto(session, False)
            release.set()
            await task
            assert not [call for call in bridge.calls if call[0] == 'start']
            assert not session.config.monitor and not session.snapshot()['auto_requested']
            auto(session)
            await session._control_tick()
            auto(session, False)
            session.accept_snapshot(ready(session))
            assert session._handle is not None and session.config.monitor
            assert not session.snapshot()['auto_enabled'] and not session.snapshot()['auto_requested']
            await session._control_tick()
            assert len([call for call in bridge.calls if call[0] == 'start']) == 1
        finally:
            release.set()
            await session.close()

    asyncio.run(scenario())


def test_service_expiry_and_reconnect_never_renew_budget_deadline_or_start_again():
    async def scenario():
        bridge = FakeBridge()
        session, clock, _ = await setup(bridge=bridge)
        try:
            auto(session)
            await session._control_tick()
            started, reserved, caption_deadline = session._monitor_started_at, session._reserved_compute_usd, session._caption_deadline
            clock.value += 121
            session.receive('owner', playback(session, position=244.4))
            await session._control_tick()
            assert session.state == 'stopped' and not session.snapshot()['auto_can_start']
            assert auto(session)['code'] == 'auto_session_expired'
            session.disconnect('owner')
            session.connect('replacement')
            assert auto(session, owner='replacement')['code'] == 'auto_session_expired'
            assert session._monitor_started_at == started and session._reserved_compute_usd == reserved
            assert session._caption_deadline == caption_deadline
            assert len([call for call in bridge.calls if call[0] == 'start']) == 1
        finally:
            await session.close()

    asyncio.run(scenario())


def test_off_during_pre_submission_control_cancels_without_spending_service_budget():
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        class Bridge(FakeBridge):
            def update_control(self, *args):
                entered.set()
                assert release.wait(2)
                return super().update_control(*args)

        bridge = Bridge()
        session, _, _ = await setup(bridge=bridge)
        try:
            auto(session)
            task = asyncio.create_task(session._control_tick())
            while not entered.is_set():
                await asyncio.sleep(.001)
            auto(session, False)
            release.set()
            await task
            assert session._handle is None and session._reserved_compute_usd == 0
            assert not [call for call in bridge.calls if call[0] == 'start']
        finally:
            release.set()
            await session.close()

    asyncio.run(scenario())


def test_early_worker_stop_requires_a_new_click_and_retains_service_time_and_all_previous_reservations():
    async def scenario():
        bridge = FakeBridge()
        session, clock, _ = await setup(bridge=bridge)
        try:
            auto(session)
            await session._control_tick()
            started, reserved = session._monitor_started_at, session._reserved_compute_usd
            clock.value += 45
            session.receive('owner', playback(session, position=168.4))
            session.accept_snapshot(ready(session, state='stopped', reason='paused'))
            await session._control_tick()
            await session._control_tick()
            assert len([call for call in bridge.calls if call[0] == 'start']) == 1
            assert session.snapshot()['auto_can_start']
            old_restarts = session._restarts
            clock.value += 5
            session.receive('owner', playback(session, position=173.4))
            auto(session)
            await session._control_tick()
            specs = [call[1] for call in bridge.calls if call[0] == 'start']
            assert len(specs) == 2 and specs[-1]['max_duration_seconds'] == 70
            assert specs[-1]['max_estimated_cost_usd'] == pytest.approx(1 - reserved)
            assert session._monitor_started_at == started
            assert session._restarts == old_restarts
            assert reserved < session._reserved_compute_usd <= 1
        finally:
            await session.close()

    asyncio.run(scenario())


def test_terminal_model_error_never_becomes_startable_or_warming_when_residual_budget_would_fit():
    async def scenario():
        bridge = FakeBridge()
        session, clock, _ = await setup(bridge=bridge)
        try:
            auto(session)
            await session._control_tick()
            session.accept_snapshot(ready(session, state='error', reason='checkpoint_unavailable'))
            assert not session.snapshot()['auto_starting']
            clock.value += 45
            session.receive('owner', playback(session, position=168.4))
            await session._control_tick()
            assert session.snapshot()['auto_start_reason'] == 'model_unavailable'
            assert auto(session)['code'] == 'auto_model_unavailable'
            assert len([call for call in bridge.calls if call[0] == 'start']) == 1
        finally:
            await session.close()

    asyncio.run(scenario())


def test_insufficient_initial_service_budget_denies_without_constructing_a_bridge():
    async def scenario():
        calls = []
        session, _, _ = await setup(factory=lambda: calls.append(True), budget=.01)
        try:
            assert session.snapshot()['auto_start_reason'] == 'budget_exhausted'
            assert auto(session)['code'] == 'auto_budget_exhausted'
            await session._control_tick()
            assert not calls and session._reserved_compute_usd == 0
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('seconds,budget', [(3601, 20), (3600, 20.01)])
def test_prepared_service_caps_cannot_exceed_user_allowance(seconds, budget):
    with pytest.raises(ValueError):
        prepared = SessionConfig(monitor=True, mode='real', checkpoint=NEW_MODEL_VERSION,
            model_version=NEW_MODEL_VERSION, max_duration_seconds=seconds,
            max_estimated_cost_usd=budget, max_restarts=0)
        ReplaySession(SourceRegistry.from_env({'S3_BUCKET_NAME': 'match-switcher-poc-videos', 'MATCHPULSE_SOURCE_ACCESS': 'public'}), prepared_config=prepared)


def test_authorized_forty_minute_prepared_service_is_available_without_spending_or_starting():
    async def scenario():
        session, _, _ = await setup(seconds=2400, budget=10)
        try:
            assert session.snapshot()['auto_can_start'] and not session.snapshot()['auto_starting']
            assert session._prepared_config.max_duration_seconds == 2400
            assert session._prepared_config.max_estimated_cost_usd == 10
            assert session._reserved_compute_usd == 0 and session._handle is None
        finally:
            await session.close()

    asyncio.run(scenario())


def test_explicit_twenty_dollar_allowance_reaches_worker_spec_without_toggle_renewal():
    async def scenario():
        bridge = FakeBridge()
        session, _, _ = await setup(bridge=bridge, seconds=2400, budget=20)
        try:
            assert session.snapshot()['auto_can_start'] and not bridge.calls
            assert auto(session) is None
            await session._control_tick()
            starts = [call[1] for call in bridge.calls if call[0] == 'start']
            assert len(starts) == 1
            assert starts[0]['max_estimated_cost_usd'] == 20
            assert starts[0]['max_duration_seconds'] == 2400
            assert session.config.max_estimated_cost_usd == session._prepared_config.max_estimated_cost_usd == 20
            started, reserved = session._monitor_started_at, session._reserved_compute_usd
            assert 0 < reserved <= 20
            auto(session, False)
            auto(session)
            await session._control_tick()
            assert len([call for call in bridge.calls if call[0] == 'start']) == 1
            assert session._monitor_started_at == started and session._reserved_compute_usd == reserved
        finally:
            await session.close()

    asyncio.run(scenario())
