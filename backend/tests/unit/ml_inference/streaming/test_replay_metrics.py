"""Offline tests for unit ml_inference streaming test_replay_metrics."""


from touchline.ml_inference.streaming.metrics import ResourceSampler


def test_resource_samples_report_mean_max_and_memory_peak():
    samples = iter([(20, 1000), (80, 900), (50, 1400)])
    sampler = ResourceSampler(query=lambda: next(samples))
    for _ in range(3):
        sampler.sample()
    assert sampler.stats == {
        'gpu_samples': 3, 'gpu_query_errors': 0,
        'gpu_utilization_mean_percent': 50,
        'gpu_utilization_max_percent': 80, 'gpu_memory_max_mib': 1400,
    }


def test_resource_query_failure_is_counted_without_fabricating_a_measurement():
    def unavailable():
        raise OSError('Private diagnostic text must not enter telemetry')

    sampler = ResourceSampler(query=unavailable)
    sampler.sample()
    assert sampler.stats == {
        'gpu_samples': 0, 'gpu_query_errors': 1,
        'gpu_utilization_mean_percent': None,
        'gpu_utilization_max_percent': None, 'gpu_memory_max_mib': None,
    }
    assert 'Private diagnostic' not in str(sampler.stats)
