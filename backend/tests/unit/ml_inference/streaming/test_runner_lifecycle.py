"""Offline tests for unit ml_inference streaming test_runner_lifecycle."""


import pytest
from tests.support.ml_inference.runner import Harness


def test_pause_freezes_processing_and_expires_idle_worker(monkeypatch):
    harness = Harness(monkeypatch, matches=1, seconds=60)
    harness.control_override = lambda h: {'playing': False, 'position': 0}
    result = harness.run()
    assert result['reason'] == 'paused'
    assert not harness.detected
    assert all(c.closed for c in harness.cursors)
    assert 30 <= harness.clock.now < 31


def test_lease_expiry_stops_without_processing_future_media(monkeypatch):
    harness = Harness(monkeypatch, matches=1)
    harness.control_override = lambda h: {'expires_at': 1000.05}
    result = harness.run()
    assert result['reason'] == 'lease_expired'
    assert all(c.closed for c in harness.cursors)
    assert result['metrics']['windows'] == 0


def test_seek_epoch_discards_pending_observations_and_uses_new_start_index(monkeypatch):
    harness = Harness(monkeypatch, matches=1)

    def control(h):
        if h.clock.now < 0.5:
            return {'clock_epoch': 0}
        return {'clock_epoch': 1, 'position': 20 + h.clock.now - 0.5,
                'active': not any(f.assembled for f in h.features[1:])}

    harness.control_override = control
    result = harness.run()
    assert len(harness.features) == 2
    assert harness.features[0].assembled == []
    assert harness.features[1].assembled == [375]
    assert harness.features[1].appended[0] == ('1:0', 300)
    assert result['scores'][0]['clock_epoch'] == 1
    assert result['scores'][0]['observation_end'] == 25
    assert all(c.closed for c in harness.cursors)


def test_timestamp_gap_recovery_uses_a_new_epoch_and_no_cross_gap_window(monkeypatch):
    harness = Harness(monkeypatch, matches=1)
    harness.fail_first_reader = True
    harness.control_override = lambda h: {'active': not any(f.assembled for f in h.features)}
    result = harness.run()
    features = harness.features[0]
    assert features.epochs == ['0:0', '0:1']
    assert features.assembled and features.assembled[0] >= 135
    assert result['metrics']['decoder_failures'] == 1
    assert any(p['health'].get('match-0', {}).get('status') == 'error' for p in harness.published)
    assert all(c.closed for c in harness.cursors)


def test_permanent_open_failure_is_bounded_and_does_not_log_source_credentials(monkeypatch):
    harness = Harness(monkeypatch, matches=1)
    harness.always_fail_open = True
    result = harness.run()
    assert result['reason'] == 'all_sources_ended'
    assert result['metrics']['decoder_failures'] == 3
    assert result['scores'] == []
    assert 'never-log-this' not in str(harness.published)
    assert 'private.invalid' not in str(harness.published)
    assert not any(p['state'] == 'ready' for p in harness.published)


def test_backward_clock_requires_an_explicit_new_epoch_and_still_closes_reader(monkeypatch):
    harness = Harness(monkeypatch, matches=1)
    harness.control_override = lambda h: {'position': h.clock.now if h.clock.now < 1 else 0}
    with pytest.raises(RuntimeError, match='Replay session failed'):
        harness.run()
    assert all(c.closed for c in harness.cursors)
    assert harness.published[-1]['state'] == 'error'
