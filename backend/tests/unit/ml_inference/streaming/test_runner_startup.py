"""Offline tests for unit ml_inference streaming test_runner_startup."""


from concurrent.futures import Future
from touchline.ml_inference.streaming import runner as replay_runner
from tests.support.ml_inference.runner import InlinePool, Harness


def test_transient_prime_failure_closes_cursor_and_cannot_release_initial_ready_gate(monkeypatch):
    harness = Harness(monkeypatch)
    harness.fail_first_prime = True
    harness.control_override = lambda h: {'position': 0, 'playing': False, 'active': h.clock.now < 2}
    result = harness.run()
    assert harness.cursors[0].closed and harness.cursors[0].prime_positions == [0]
    assert all(c.closed for c in harness.cursors)
    ready = [p for p in harness.published if p['state'] == 'ready']
    assert ready and all(p['produced_at'] >= 1001 for p in ready)
    assert result['metrics']['decoder_failures'] == 1
    assert not harness.detected
    assert 'never-log-this' not in str(harness.published)


def test_slow_current_point_prime_can_finish_after_thirty_seconds_before_ready(monkeypatch):
    harness = Harness(monkeypatch, matches=1, seconds=100)
    original_prime = harness.cursor_factory.prime

    def slow_prime(cursor, permit):
        harness.clock.now += 35
        return original_prime(cursor, permit)

    monkeypatch.setattr(harness.cursor_factory, 'prime', slow_prime)
    harness.control_override = lambda h: {
        'position': 0, 'playing': False,
        'active': not any(p['state'] == 'ready' for p in h.published),
    }
    result = harness.run()
    assert result['reason'] == 'control_stopped'
    assert any(p['state'] == 'ready' and p['produced_at'] >= 1035 for p in harness.published)
    assert not harness.detected


def test_unready_initialization_has_a_separate_bounded_timeout(monkeypatch):
    harness = Harness(monkeypatch, matches=1, seconds=5)

    class UnreadyPool(InlinePool):
        def submit(self, function, *args, **kwargs):
            return Future()

    monkeypatch.setattr(replay_runner, 'ThreadPoolExecutor', UnreadyPool)
    harness.control_override = lambda h: {'position': 0, 'playing': False}
    result = harness.run()
    assert result['state'] == 'error' and result['reason'] == 'startup_timeout'
    assert not harness.detected


def test_future_rounded_seek_base_is_not_primed_before_its_permission_clock(monkeypatch):
    harness = Harness(monkeypatch, matches=1)
    harness.control_override = lambda h: {'position': 1, 'active': h.clock.now < .1}
    harness.run()
    assert harness.cursors[0].next_index == 30  # Rounded two-second start is still future.
    assert harness.cursors[0].prime_positions == []
    assert harness.features[0].appended == []
