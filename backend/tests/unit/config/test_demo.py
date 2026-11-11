"""Offline tests for unit config test_demo."""


import asyncio
import json
from types import SimpleNamespace
import pytest
from touchline import demo
from touchline.demo import DemoBudget, configuration
from touchline.ml_inference.models.release import LEGACY_MODEL_VERSION, NEW_MODEL_VERSION


@pytest.fixture
def finite_demo(monkeypatch, tmp_path):
    """Exercise the actual serve lifecycle with fake inference and a fast clock."""
    clock, events = SimpleNamespace(value=0.0), []
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        clock.value += seconds
        await real_sleep(0)

    class Session:
        state, position, playing, clock_epoch = 'running', 7.0, True, 0

        def __init__(self):
            self.clients = {'viewer'}
            self.metrics = {'reserved_compute_usd': .1, 'worker_stop_succeeded': False}
            self.worker_health = {'cam-1': {'status': 'healthy'}}
            self.director = SimpleNamespace(current_match_id='cam-1', state=lambda **kwargs: {})
            self.closed = False

        def now(self):
            return 1000 + clock.value

        async def close(self):
            self.closed = True
            self.metrics['worker_stop_succeeded'] = True
            events.append(('owned_session_closed', clock.value))

    session = Session()

    class Controller:
        def __init__(self, **kwargs):
            self.session = session

        async def start(self):
            events.append(('server_started', clock.value))
            try:
                await asyncio.Event().wait()
            finally:
                assert session.closed  # Owned inference is revoked before socket drain.
                events.append(('socket_drained', clock.value))

    monkeypatch.setattr(demo, 'AppController', Controller)
    monkeypatch.setattr(demo, 'ARTIFACTS', tmp_path)
    monkeypatch.setattr(demo, 'load_local_environment', lambda: None)
    monkeypatch.setattr(demo, 'cloud_environment', dict)
    monkeypatch.setattr(demo, 'time', SimpleNamespace(perf_counter=lambda: clock.value,
                                                    time=lambda: 1000 + clock.value))
    monkeypatch.setattr(demo.asyncio, 'sleep', fast_sleep)
    monkeypatch.setenv('MATCHPULSE_ALLOWED_ORIGINS', 'http://localhost:3000')
    monkeypatch.setenv('MATCHPULSE_API_ALLOWED_ORIGINS', 'http://localhost:3000')
    args = SimpleNamespace(matches=7, seconds=30, offline=False, model='selected',
                           director_mode='balanced', budget=1, port=8030,
                           frontend_port=3030, api_port=8031)
    return args, session, events, clock


@pytest.mark.parametrize("matches,workers", [(1, 1), (2, 1), (3, 2), (7, 4)])
def test_demo_uses_distinct_existing_sources_and_bounded_real_inference(matches, workers):
    registry, config, allowance = configuration(matches, 120, False)
    sources = registry.resolve()
    assert [source.match_id for source in sources] == [f"cam-{i}" for i in range(1, matches + 1)]
    assert len({source.source_url for source in sources}) == matches
    assert [source.key for source in sources] == [f"match-{i}.mp4" for i in range(1, matches + 1)]
    assert all(source.offset_seconds == 120 for source in sources)
    assert config.mode == "real" and config.mock_scores is None and config.max_restarts == 0
    assert config.model_version == config.checkpoint == NEW_MODEL_VERSION
    assert config.director_mode == "balanced"
    assert config.gpu_workers == workers and 0 < allowance < 2


def test_offline_demo_cannot_start_monitoring():
    _, config, _ = configuration(7, 120, True)
    assert not config.monitor and config.mode == "disabled" and config.checkpoint is None
    assert config.model_version == "disabled"


@pytest.mark.parametrize("mode", ["relaxed", "balanced", "all_action"])
def test_explicit_rollback_is_versioned_and_keeps_selected_director_mode(mode):
    _, config, _ = configuration(7, 120, False, model="legacy", director_mode=mode)
    assert config.checkpoint == config.model_version == LEGACY_MODEL_VERSION
    assert config.checkpoint != NEW_MODEL_VERSION
    assert config.director_mode == mode and config.mock_scores is None


def test_per_launch_budget_keeps_actual_source_allocation_bounded():
    _, config, required = configuration(7, 120, False, budget_usd=1)
    assert config.max_estimated_cost_usd == 1 and config.gpu_workers == 4
    assert 0 < required < 1
    with pytest.raises(ValueError, match="per-launch budget"):
        configuration(7, 120, False, budget_usd=0.01)


@pytest.mark.parametrize("budget", [True, 0, -1, float("nan"), float("inf"), 3.01, "1"])
def test_invalid_per_launch_budget_is_rejected_without_starting_a_worker(budget):
    with pytest.raises(ValueError, match="budget"):
        configuration(7, 120, False, budget_usd=budget)


def test_unknown_model_and_director_mode_cannot_silently_fall_back():
    with pytest.raises(ValueError, match="model"):
        configuration(7, 120, False, model="missing-model")
    with pytest.raises(ValueError, match="DIRECTOR_MODE"):
        configuration(7, 120, False, director_mode="unknown")


@pytest.mark.parametrize("matches,seconds", [(0, 120), (8, 120), (True, 120), (7, 29), (7, 181)])
def test_demo_rejects_unbounded_arguments(matches, seconds):
    with pytest.raises(ValueError):
        configuration(matches, seconds, False)


def test_verification_budget_is_persistent_and_keeps_uncertain_stop_reserved(tmp_path):
    ledger = DemoBudget(tmp_path)
    reservation = ledger.reserve(1.5)
    ledger.settle(reservation, 10, 4, started=True, stopped=False)
    reloaded = DemoBudget(tmp_path)
    with pytest.raises(RuntimeError, match="allowance"):
        reloaded.reserve(0.51)
    ledger.settle(reservation, 10, 4, started=True, stopped=True)
    assert 0 < json.loads((tmp_path / "budget.json").read_text())["estimated_usd"] < 1.5


def test_no_viewer_does_not_consume_gpu_estimate(tmp_path):
    ledger = DemoBudget(tmp_path)
    reservation = ledger.reserve(0.5)
    ledger.settle(reservation, 120, 4, started=False, stopped=True)
    assert ledger.data["estimated_usd"] == 0


def test_progress_permission_error_does_not_end_viewing_and_final_receipt_cleanup_remain_strict(
        finite_demo, monkeypatch, tmp_path, caplog):
    args, session, events, clock = finite_demo
    actual_write, progress_attempts = demo.write_receipt, []

    def write_with_locked_progress(directory, name, value):
        if name == 'browser-progress.json':
            progress_attempts.append(clock.value)
            if len(progress_attempts) == 1:
                raise PermissionError('private source URL or local path must not enter logs')
        actual_write(directory, name, value)

    monkeypatch.setattr(demo, 'write_receipt', write_with_locked_progress)
    asyncio.run(demo.serve(args))
    assert len(progress_attempts) == 15 and progress_attempts[0] == 2
    assert clock.value == args.seconds == 30
    assert session.closed and session.metrics['worker_stop_succeeded'] is True
    assert events[-2:] == [('owned_session_closed', 30), ('socket_drained', 30)]
    run = next((tmp_path / 'runs').iterdir())
    report = json.loads((run / 'browser.json').read_text())
    assert report['elapsed_seconds'] == 30 and report['socket_shutdown'] == 'completed'
    assert report['progress_receipt_failures'] == 1 and len(report['observations']) == 15
    assert report['metrics']['worker_stop_succeeded'] is True
    assert json.loads((run / 'budget.json').read_text())['entries'][0]['state'] == 'stopped'
    assert 'PermissionError' in caplog.text and 'continuing' in caplog.text
    assert 'private source URL' not in caplog.text


def test_final_receipt_permission_error_still_fails_after_owned_cleanup_and_socket_drain(
        finite_demo, monkeypatch, tmp_path):
    args, session, events, _ = finite_demo
    actual_write = demo.write_receipt

    def fail_final_receipt(directory, name, value):
        if name == 'browser.json':
            raise PermissionError('Final audit could not be written')
        actual_write(directory, name, value)

    monkeypatch.setattr(demo, 'write_receipt', fail_final_receipt)
    with pytest.raises(PermissionError, match='Final audit'):
        asyncio.run(demo.serve(args))
    assert session.closed and events[-2:] == [('owned_session_closed', 30), ('socket_drained', 30)]
    run = next((tmp_path / 'runs').iterdir())
    assert not (run / 'browser.json').exists()
    assert json.loads((run / 'budget.json').read_text())['entries'][0]['state'] == 'reserved'
