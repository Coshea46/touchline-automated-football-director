"""Offline tests for unit captions live test_limits."""


import asyncio
import pytest
from touchline.captions.limits import DEFAULT_AUDIO_SECONDS, DEFAULT_COST_USD, LIVE_PRICE_PER_HOUR_USD, wall_allowance_seconds
from touchline.captions.live.engine import RealtimeEngine
from touchline.captions.live.adapter import RealtimeCaptionWorker
from touchline.main import AppController
from touchline.replay import ReplaySession, SourceRegistry
from touchline.replay.config import SessionConfig
from tests.support.captions.session import CaptionWorker, toggle
from tests.support.replay.session import Clock, FakeBridge, playback


def test_backend_adapter_and_child_defaults_share_authorized_eight_hour_caps(monkeypatch):
    monkeypatch.delenv('MATCHPULSE_CAPTION_MAX_AUDIO_SECONDS', raising=False)
    monkeypatch.delenv('MATCHPULSE_CAPTION_BUDGET_USD', raising=False)
    controller = AppController(config=SessionConfig(), api_enabled=False)
    assert controller.session.caption_state()['max_audio_seconds'] == DEFAULT_AUDIO_SECONDS == 28800
    assert controller.session.caption_state()['max_cost_usd'] == DEFAULT_COST_USD == 3.60
    worker = RealtimeCaptionWorker(lambda event: None)
    engine = RealtimeEngine(lambda: None, lambda event: None, audio_factory=object)
    try:
        assert float(worker.env['MATCHPULSE_CAPTION_MAX_AUDIO_SECONDS']) == engine.max_audio == 28800
        assert float(worker.env['MATCHPULSE_CAPTION_BUDGET_USD']) == engine.max_cost == 3.60
        assert engine.metrics['provider_sessions'] == 0
        assert 28800 / 3600 * LIVE_PRICE_PER_HOUR_USD == pytest.approx(3.12)
        assert wall_allowance_seconds(28800, 3.60) == 28800
    finally:
        assert worker.close() is True
        engine._executor.shutdown(wait=False)


@pytest.mark.parametrize('layer', ['session', 'adapter', 'engine', 'controller'])
@pytest.mark.parametrize('seconds,cost', [(28801, 3.60), (28800, 3.601), (float('nan'), 3.60),
                                       (28800, float('inf')), (True, 3.60), (28800, True)])
def test_every_entry_point_rejects_excessive_or_nonfinite_caption_allowances(monkeypatch, layer, seconds, cost):
    with pytest.raises((ValueError, TypeError)):
        if layer == 'session':
            ReplaySession(SourceRegistry.from_env({'S3_BUCKET_NAME': 'match-switcher-poc-videos', 'MATCHPULSE_SOURCE_ACCESS': 'public'}), caption_max_audio_seconds=seconds, caption_max_cost_usd=cost)
        elif layer == 'adapter':
            RealtimeCaptionWorker(lambda event: None, max_audio_seconds=seconds, max_cost_usd=cost)
        elif layer == 'engine':
            RealtimeEngine(lambda: None, lambda event: None, max_audio_seconds=seconds,
                           max_cost_usd=cost, audio_factory=object)
        else:
            monkeypatch.setenv('MATCHPULSE_CAPTION_MAX_AUDIO_SECONDS', str(seconds))
            monkeypatch.setenv('MATCHPULSE_CAPTION_BUDGET_USD', str(cost))
            AppController(config=SessionConfig(), api_enabled=False)


def test_eight_hour_deadline_never_renews_across_off_switch_seek_disconnect_and_on():
    async def scenario():
        clock, worker = Clock(), CaptionWorker()
        session = await ReplaySession(SourceRegistry.from_env({'S3_BUCKET_NAME': 'match-switcher-poc-videos', 'MATCHPULSE_SOURCE_ACCESS': 'public', 'MATCHPULSE_MATCH_COUNT': '2'}),
            config=SessionConfig(), bridge=FakeBridge(), caption_worker=worker,
            now=clock, monotonic=clock, session_id='session').initialize()
        session.connect('owner')
        try:
            assert session.caption_state()['deadline'] is None
            session.receive('owner', playback(session, position=5))
            deadline = session.caption_state()['deadline']
            assert deadline == clock.value + 28800
            toggle(session, False)
            clock.value += 3600
            session.receive('owner', {'type': 'select', 'session_id': 'session', 'match_id': 'cam-2'})
            session.receive('owner', playback(session, match_id='cam-2', position=1))
            session.disconnect('owner')
            session.connect('replacement')
            toggle(session, True, owner='replacement')
            assert session.caption_state()['deadline'] == deadline
            assert session.caption_worker is worker
            clock.value = deadline - 1
            session.receive('replacement', playback(session, match_id='cam-2', position=2))
            assert worker.updates[-1]['deadline'] == deadline and session.caption_state()['enabled']
            count = len(worker.updates)
            clock.value = deadline
            session._update_captions()
            assert session.caption_state()['status'] == 'stopped'
            assert session.caption_state()['reason'] == 'budget_deadline'
            assert toggle(session, True, owner='replacement')['code'] == 'captions_budget_exhausted'
            assert session.caption_state()['deadline'] == deadline and len(worker.updates) == count
        finally:
            await session.close()

    asyncio.run(scenario())


def test_smaller_cost_ceiling_shortens_real_session_deadline_even_with_eight_hour_time_cap():
    async def scenario():
        clock, worker = Clock(), CaptionWorker()
        session = await ReplaySession(SourceRegistry.from_env({'S3_BUCKET_NAME': 'match-switcher-poc-videos', 'MATCHPULSE_SOURCE_ACCESS': 'public', 'MATCHPULSE_MATCH_COUNT': '1'}),
            config=SessionConfig(), bridge=FakeBridge(), caption_worker=worker,
            caption_max_audio_seconds=28800, caption_max_cost_usd=.01,
            now=clock, monotonic=clock, session_id='session').initialize()
        session.connect('owner')
        try:
            session.receive('owner', playback(session, position=5))
            allowance = session.caption_state()['deadline'] - clock.value
            assert allowance == pytest.approx(.01 * 3600 / LIVE_PRICE_PER_HOUR_USD)
            assert allowance < 28800
            assert allowance / 3600 * LIVE_PRICE_PER_HOUR_USD == pytest.approx(.01)
            clock.value += allowance
            session._update_captions()
            assert session.caption_state()['reason'] == 'budget_deadline'
        finally:
            await session.close()

    asyncio.run(scenario())


def test_child_counts_prior_generations_against_eight_hour_connected_time_without_waiting():
    clock = [0.0]
    engine = RealtimeEngine(lambda: None, lambda event: None, audio_factory=object, now=lambda: clock[0])
    try:
        engine.metrics['provider_connected_seconds'] = 28700
        engine._connected_since = 0.0
        clock[0] = 99.9
        assert not engine._over_budget()
        clock[0] = 100
        assert engine._over_budget()
        assert engine._totals()['provider_connected_seconds'] == 28800
        assert engine._totals()['estimated_cost_usd'] == pytest.approx(3.12)
        assert engine._totals()['estimated_cost_usd'] < engine.max_cost
    finally:
        engine._executor.shutdown(wait=False)


def test_child_counts_prior_generations_against_eight_hour_pcm_limit_without_waiting():
    engine = RealtimeEngine(lambda: None, lambda event: None, audio_factory=object)
    try:
        engine.metrics['pcm_seconds'] = 28800 - .25
        assert not engine._over_budget()
        engine.metrics['pcm_seconds'] += .25
        assert engine._over_budget()
        assert engine._totals()['pcm_seconds'] == 28800
        assert engine._totals()['provider_sessions'] == 0
    finally:
        engine._executor.shutdown(wait=False)
