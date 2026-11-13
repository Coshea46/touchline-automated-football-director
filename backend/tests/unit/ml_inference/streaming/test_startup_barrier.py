"""Offline tests for unit ml_inference streaming test_startup_barrier."""


from threading import Event
import pytest
from touchline.ml_inference.streaming.client import ReplayClient
from touchline.ml_inference.streaming.contract import validate_control
from touchline.ml_inference.streaming.ws_client import ReplayWebSocketClient, _SocketHandle
from tests.support.ml_inference.client import control, sdk_fixture, session
from tests.support.ml_inference.runner import Harness


@pytest.mark.parametrize('hold_seconds', [45, 70.4, 105])
def test_fast_ready_shard_waits_for_cohort_or_tutorial_then_scores_without_restart(monkeypatch, hold_seconds):
    harness = Harness(monkeypatch, matches=1, seconds=120)
    harness.control_override = lambda h: {
        'position': max(0, h.clock.now - hold_seconds), 'playing': h.clock.now >= hold_seconds,
        'startup_waiting': h.clock.now < hold_seconds,
        'active': not any(f.assembled for f in h.features),
    }
    result = harness.run()
    assert result['reason'] == 'control_stopped'
    assert hold_seconds + 5 <= harness.clock.now < hold_seconds + 6
    assert len(harness.cursors) == len(harness.features) == 1
    assert harness.features[0].assembled == [75]
    assert result['scores'][0]['observation_end'] == 5
    assert result['metrics']['detector_frames'] == 75
    assert all(p['metrics']['detector_frames'] == 0 for p in harness.published
               if p['produced_at'] < 1000 + hold_seconds)
    assert all(c.closed for c in harness.cursors)


def test_barrier_release_resets_idle_clock_instead_of_immediately_expiring(monkeypatch):
    harness = Harness(monkeypatch, matches=1, seconds=90)
    harness.control_override = lambda h: {
        'position': 0, 'playing': False, 'startup_waiting': h.clock.now < 45,
    }
    result = harness.run()
    assert result['reason'] == 'paused'
    assert 74.9 <= harness.clock.now < 75.1
    assert result['scores'] == [] and not harness.detected


@pytest.mark.parametrize('duration,limit', [(60, 60), (180, 120)])
def test_stuck_barrier_is_bounded_even_if_permitted_position_continues_advancing(monkeypatch, duration, limit):
    harness = Harness(monkeypatch, matches=1, seconds=duration)
    harness.control_override = lambda h: {'startup_waiting': True}
    result = harness.run()
    assert result['state'] == 'error' and result['reason'] == 'startup_timeout'
    assert limit <= harness.clock.now < limit + .1
    assert all(c.closed for c in harness.cursors)


def test_waiting_barrier_never_overrides_expired_viewer_lease(monkeypatch):
    harness = Harness(monkeypatch, matches=1, seconds=90)
    harness.control_override = lambda h: {'startup_waiting': True, 'position': 0, 'expires_at': 1005}
    result = harness.run()
    assert result['reason'] == 'lease_expired' and 5 <= harness.clock.now < 5.1
    assert result['scores'] == [] and all(c.closed for c in harness.cursors)


@pytest.mark.parametrize('present', [False, True])
def test_legacy_or_explicit_released_controls_retain_thirty_second_pause_guard(monkeypatch, present):
    harness = Harness(monkeypatch, matches=1, seconds=90)
    harness.control_override = lambda h: {
        'position': 0, 'playing': False, **({'startup_waiting': False} if present else {}),
    }
    result = harness.run()
    assert result['reason'] == 'paused' and 30 <= harness.clock.now < 30.1


@pytest.mark.parametrize('bad', [None, 0, 1, 'true', [], {}])
def test_startup_waiting_requires_an_explicit_boolean(bad):
    with pytest.raises(ValueError, match='startup_waiting'):
        validate_control('owned-session', control(startup_waiting=bad))


def test_optional_barrier_flag_survives_dict_and_websocket_control_paths_unchanged(monkeypatch):
    sdk, store, _, _, _ = sdk_fixture(monkeypatch)
    dictionary = ReplayClient(sdk=sdk)
    websocket = ReplayWebSocketClient('ws://127.0.0.1:1/session', 'fixture-token', allow_loopback=True)
    socket_handle = object.__new__(_SocketHandle)
    socket_handle._owner, socket_handle._spec = websocket, session()
    socket_handle._stop = Event()
    for waiting in (True, False):
        packet = control(startup_waiting=waiting)
        dictionary.update_control('owned-session', packet)
        websocket.update_control('owned-session', packet)
        assert store.values['control:owned-session']['startup_waiting'] is waiting
        assert socket_handle._control()['startup_waiting'] is waiting
        assert socket_handle._control()['position'] == packet['position']
        assert socket_handle._control()['expires_at'] == packet['expires_at']
