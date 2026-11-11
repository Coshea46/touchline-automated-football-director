"""Offline tests for unit ml_inference streaming test_runner_parallelism."""


from concurrent.futures import Future
from touchline.ml_inference.streaming import runner as replay_runner
from touchline.ml_inference.features.media import MediaTimelineError
from tests.support.ml_inference.runner import InlinePool, Harness, delayed_visual_pool


def test_epoch_change_closes_a_completed_open_that_was_not_yet_harvested(monkeypatch):
    class DelayedVisibility(Future):
        def __init__(self):
            super().__init__()
            self.checks = 0

        def done(self):
            self.checks += 1
            return self.checks > 1

    class DelayedOpenPool(InlinePool):
        def submit(self, function, *args, **kwargs):
            future = DelayedVisibility()
            try:
                future.set_result(function(*args, **kwargs))
            except Exception as exc:  # noqa: BLE001 -- reproduce Future exception transport.
                future.set_exception(exc)
            return future

    harness = Harness(monkeypatch, matches=1)
    monkeypatch.setattr(replay_runner, 'ThreadPoolExecutor', DelayedOpenPool)
    harness.control_override = lambda h: {
        'active': h.clock.now < .04, 'clock_epoch': int(h.clock.now > .005),
    }
    harness.run()
    assert len(harness.cursors) == 2
    assert all(c.closed for c in harness.cursors)


def test_next_detector_batch_runs_before_waiting_for_previous_visual_measurements(monkeypatch):
    harness = Harness(monkeypatch)
    futures, events, _ = delayed_visual_pool(harness, monkeypatch)
    harness.control_override = lambda h: {
        'position': 0 if h.clock.now == 0 else 9,
        'active': not (len(h.features) == 2 and all(len(f.assembled) == 3 for f in h.features)),
    }
    result = harness.run()
    first_submit = next(count for event, count in events if event == 'visual_submit')
    first_wait = next(count for event, count in events if event == 'visual_result')
    assert first_submit == 16 and first_wait >= 32  # Another YOLO batch overlapped CPU work.
    assert all(f.assembled == [75, 105, 135] for f in harness.features)
    assert len(harness.detected) == len(set(harness.detected)) == 270
    assert result['metrics']['max_visual_pending'] <= 2
    assert result['metrics']['max_queue_samples'] <= 32
    assert all(Future.done(f) for f in futures)
    for payload in harness.published:
        for health in payload['health'].values():
            assert health['next_sample_index'] <= health['scheduled_sample_index']
            if health['visual_pending_samples']:
                assert health['next_sample_index'] < health['scheduled_sample_index']


def test_stop_cancels_pending_cpu_work_without_waiting_or_claiming_it_was_processed(monkeypatch):
    harness = Harness(monkeypatch, matches=1)
    futures, _, shutdowns = delayed_visual_pool(harness, monkeypatch, delay=100)
    harness.control_override = lambda h: {
        'position': 0 if h.clock.now == 0 else 2, 'active': h.clock.now < .03,
    }
    result = harness.run()
    assert futures and all(f.cancelled() for f in futures)
    assert harness.features[0].appended == []
    health = result['health']['match-0']
    assert health['next_sample_index'] == 0 and health['scheduled_sample_index'] == 16
    assert health['visual_pending_samples'] == 16
    assert (True, {'wait': False, 'cancel_futures': True}) in shutdowns
    assert all(c.closed for c in harness.cursors)


def test_seek_discards_old_pending_cpu_job_before_new_epoch_features_are_used(monkeypatch):
    harness = Harness(monkeypatch, matches=1)
    futures, _, _ = delayed_visual_pool(harness, monkeypatch, delay=100)

    def control(h):
        return {
            'clock_epoch': int(h.clock.now >= .03), 'active': h.clock.now < .06,
            'position': (0 if h.clock.now == 0 else 2) if h.clock.now < .03 else 20.5,
        }

    harness.control_override = control
    result = harness.run()
    assert len(harness.features) == 2
    assert harness.features[0].appended == []
    assert futures[0].cancelled()
    assert result['clock_epoch'] == 1
    assert result['health']['match-0']['next_sample_index'] == 330
    assert all(c.closed for c in harness.cursors)


def test_visual_failure_discards_detected_followup_rows_and_recovers_in_a_new_epoch(monkeypatch):
    harness = Harness(monkeypatch, matches=1)
    delayed_visual_pool(harness, monkeypatch)
    original_append = harness.feature_factory.append_batch
    failed = False

    def append(features, *args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise MediaTimelineError('Measurement failure')
        return original_append(features, *args, **kwargs)

    monkeypatch.setattr(harness.feature_factory, 'append_batch', append)
    harness.control_override = lambda h: {'active': not any(f.assembled for f in h.features)}
    result = harness.run()
    features = harness.features[0]
    assert features.epochs == ['0:0', '0:1']
    assert all(epoch == '0:1' for epoch, index in features.appended)
    assert result['metrics']['visual_failures'] == 1
    assert result['metrics']['source_failure_types'] == {'visual:MediaTimelineError': 1}
    assert result['health']['match-0']['last_error_phase'] == 'visual'
    assert result['health']['match-0']['last_error_type'] == 'MediaTimelineError'


def test_reader_failure_waits_for_running_visual_job_before_resetting_mutable_features(monkeypatch):
    harness = Harness(monkeypatch, matches=1)
    harness.fail_first_reader = True
    futures, _, _ = delayed_visual_pool(harness, monkeypatch, delay=2)

    def control(h):
        for future in futures:
            # Mirror CPU jobs that have begun: cancel cannot interrupt them.
            future.cancel = lambda: False
        return {
            'position': 0 if h.clock.now == 0 else 4 + max(0, h.clock.now - 1.5),
            'active': not any(f.assembled for f in h.features),
        }

    original_reset = harness.feature_factory.reset_epoch

    def reset(features, epoch):
        assert not getattr(features, '_test_visual_busy', False)
        assert all(Future.done(future) for future in futures)
        return original_reset(features, epoch)

    monkeypatch.setattr(harness.feature_factory, 'reset_epoch', reset)
    harness.control_override = control
    result = harness.run()
    assert result['reason'] == 'control_stopped'
    assert harness.features[0].epochs == ['0:0', '0:1']
    assert harness.features[0].assembled[0] >= 165
    assert result['metrics']['source_failure_types'] == {'reader:MediaTimelineError': 1}
    assert result['health']['match-0']['last_error_phase'] == 'reader'


def test_ready_windows_are_all_submitted_before_waiting_for_the_first_assembly(monkeypatch):
    harness = Harness(monkeypatch)
    events = []

    class AssemblyFuture(Future):
        def __init__(self, function, args, kwargs):
            super().__init__()
            self.function, self.args, self.kwargs = function, args, kwargs

        def result(self, timeout=None):
            if not self.done():
                events.append(('wait', self.args[0].source['match_id']))
                try:
                    self.set_result(self.function(*self.args, **self.kwargs))
                except Exception as exc:  # noqa: BLE001 -- reproduce Future exception transport.
                    self.set_exception(exc)
            return super().result(timeout=timeout)

    class AssemblyPool(InlinePool):
        def submit(self, function, *args, **kwargs):
            if function.__name__ != 'assemble_visual':
                return super().submit(function, *args, **kwargs)
            events.append(('submit', args[0].source['match_id']))
            return AssemblyFuture(function, args, kwargs)

    monkeypatch.setattr(replay_runner, 'ThreadPoolExecutor', AssemblyPool)
    harness.control_override = lambda h: {
        'active': not (len(h.features) == 2 and all(f.assembled for f in h.features)),
    }
    result = harness.run()
    assert events[:4] == [('submit', 'match-0'), ('submit', 'match-1'),
                          ('wait', 'match-0'), ('wait', 'match-1')]
    assert result['metrics']['max_assembly_pending'] == 2
    assert all(f.assembled == [75] for f in harness.features)
    assert result['metrics']['windows'] == 2
    assert all(c.closed for c in harness.cursors)


def test_failed_parallel_assembly_cannot_emit_a_score_or_stop_a_healthy_match(monkeypatch):
    harness = Harness(monkeypatch)
    original_assemble = harness.feature_factory.assemble

    def assemble(features, end, audio):
        if features.key == 'match-0':
            raise ValueError('Window assembly failed')
        return original_assemble(features, end, audio)

    monkeypatch.setattr(harness.feature_factory, 'assemble', assemble)
    harness.control_override = lambda h: {'active': not any(f.assembled for f in h.features)}
    result = harness.run()
    assert result['metrics']['window_assembly_failures'] == 1
    assert result['metrics']['source_failure_types'] == {'assembly:ValueError': 1}
    assert result['metrics']['max_assembly_pending'] == 2
    assert result['health']['match-0']['last_error_phase'] == 'assembly'
    assert result['health']['match-0']['status'] == 'error'
    assert [score['match_id'] for score in result['scores']] == ['match-1']
    assert result['metrics']['windows'] == 1
    assert all(c.closed for c in harness.cursors)
