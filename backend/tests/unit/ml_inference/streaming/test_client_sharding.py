"""Offline tests for unit ml_inference streaming test_client_sharding."""


import copy
import pytest
from touchline.ml_inference.streaming.client import ReplayClient
from tests.support.ml_inference.client import control, sdk_fixture, multi_session, running_shard, started_shards


@pytest.mark.parametrize('count,groups', [(1, [1]), (2, [2]), (4, [2, 2]), (5, [2, 2, 1]), (7, [2, 2, 2, 1])])
def test_default_partition_is_balanced_bounded_and_has_no_independent_parameter_pools(monkeypatch, count, groups):
    sdk, store, _, specs, _ = sdk_fixture(monkeypatch)
    client = ReplayClient(sdk=sdk)
    client.update_control('owned-session', control())
    owned = client.start(multi_session(count))
    assert [len(spec['matches']) for spec in specs] == groups
    assert len(sdk.handles) == len(groups) and len(owned.handles) == len(groups)
    assert sum(spec.get('max_estimated_cost_usd', 1) for spec in specs) == 1
    if len(groups) > 1:
        assert [spec['shard_id'] for spec in specs] == [str(index) for index in range(len(groups))]
        assert all(spec['gpu_workers'] == 1 for spec in specs)
        assert owned.score_keys == tuple(f'scores:owned-session:{index}' for index in range(len(groups)))
    else:
        assert 'shard_id' not in specs[0]
    client.stop('owned-session', owned)
    assert sdk.cancelled == list(range(len(groups))) and not store.values


@pytest.mark.parametrize('workers,budgets', [(1, [10]), (4, [5, 5, 5, 5])])
def test_larger_parent_budget_keeps_child_wire_compatible_with_deployed_workers(monkeypatch, workers, budgets):
    sdk, store, _, specs, _ = sdk_fixture(monkeypatch)
    client = ReplayClient(sdk=sdk)
    client.update_control('owned-session', control())
    configured = multi_session(gpu_workers=workers, max_estimated_cost_usd=20, max_duration_seconds=3600)
    original = copy.deepcopy(configured)

    owned = client.start(configured)

    assert [spec['max_estimated_cost_usd'] for spec in specs] == budgets
    assert all(spec['max_estimated_cost_usd'] <= 10 for spec in specs)
    assert sum(spec['max_estimated_cost_usd'] for spec in specs) <= configured['max_estimated_cost_usd']
    assert configured == original
    assert len(owned.handles) == workers
    client.stop('owned-session', owned)
    assert sdk.cancelled == list(range(workers)) and not store.values


def test_explicit_three_workers_balance_seven_sources_without_dropping_a_shard(monkeypatch):
    client, sdk, store, specs, owned = started_shards(monkeypatch, gpu_workers=3)
    assert [len(spec['matches']) for spec in specs] == [3, 2, 2]
    assert len(owned.handles) == 3
    assert {source['match_id'] for spec in specs for source in spec['matches']} == {f'cam-{i}' for i in range(1, 8)}
    client.stop('owned-session', owned)
    assert sdk.cancelled == [0, 1, 2] and not store.values


def test_single_gpu_benchmark_override_retains_unsharded_transport(monkeypatch):
    _, sdk, _, specs, owned = started_shards(monkeypatch, gpu_workers=1)
    assert len(sdk.handles) == 1 and len(specs[0]['matches']) == 7
    assert 'shard_id' not in specs[0] and owned.score_keys == ('scores:owned-session',)


def test_shard_mock_scores_are_filtered_without_changing_parent_configuration(monkeypatch):
    configured = {f'cam-{i}': i / 10 for i in range(1, 8)}
    _, _, _, specs, _ = started_shards(monkeypatch, mock_scores=configured)
    assert [set(spec['mock_scores']) for spec in specs] == [{f'cam-{i}' for i in range(1, 5)},
                                                          {f'cam-{i}' for i in range(5, 8)}]
    assert len(configured) == 7


def test_owned_bundle_cancels_every_shard_even_when_the_first_cancel_fails(monkeypatch):
    client, sdk, store, _, owned = started_shards(monkeypatch)
    sdk.cancel_fail.add(0)
    with pytest.raises(RuntimeError, match='TimeoutError'):
        client.stop('owned-session', owned)
    assert sdk.cancelled == [0, 1] and not store.values and client._active is None


def test_merge_waits_for_all_ready_and_keeps_only_one_unchanged_poll_snapshot(monkeypatch):
    client, _, store, specs, owned = started_shards(monkeypatch)
    store.put(owned.score_keys[0], running_shard(specs[0], state='ready'))
    assert client.latest('owned-session') is None
    store.put(owned.score_keys[1], running_shard(specs[1], state='starting'))
    assert client.latest('owned-session')['state'] == 'starting'
    store.put(owned.score_keys[1], running_shard(specs[1], sequence=2, state='ready'))
    ready = client.latest('owned-session')
    assert ready['state'] == 'ready'
    assert client.latest('owned-session') == ready
    ready['health'].clear()
    assert len(client.latest('owned-session')['health']) == 7


def test_merge_compact_scores_preserves_endpoints_and_aggregates_metrics(monkeypatch):
    client, _, store, specs, owned = started_shards(monkeypatch)
    for key, spec in zip(owned.score_keys, specs):
        store.put(key, running_shard(spec))
    merged = client.latest('owned-session')
    assert merged['state'] == 'running' and len(merged['scores']) == 7
    assert merged['worker_sequence'] == 1 and merged['metrics']['detector_frames'] == 525
    assert merged['metrics']['detector_seconds'] == pytest.approx(.7)
    assert merged['metrics']['peak_rss_bytes'] == 4000 and merged['metrics']['total_peak_rss_bytes'] == 7000
    assert len(merged['metrics']['runtime']['workers']) == 2
    assert len(merged['metrics']['streams']) == 7
    assert {record['observation_end'] for record in merged['scores']} == {5}
    assert client.latest('owned-session')['worker_sequence'] == 1
    store.put(owned.score_keys[0], running_shard(specs[0], sequence=2, end=7))
    mixed = client.latest('owned-session')
    assert mixed['worker_sequence'] == 2
    assert {record['observation_end'] for record in mixed['scores']} == {5, 7}


@pytest.mark.parametrize('field,bad', [('session_id', 'previous'), ('model_version', 'old'),
                                     ('clock_epoch', 1), ('score_kind', 'model')])
def test_merge_cannot_combine_different_session_model_epoch_or_mode(monkeypatch, field, bad):
    client, _, store, specs, owned = started_shards(monkeypatch)
    store.put(owned.score_keys[0], running_shard(specs[0]))
    invalid = running_shard(specs[1])
    invalid[field] = bad
    store.put(owned.score_keys[1], invalid)
    assert client.latest('owned-session') is None


def test_epoch_change_discards_old_shard_cache_and_requires_both_new_epochs(monkeypatch):
    client, _, store, specs, owned = started_shards(monkeypatch)
    for key, spec in zip(owned.score_keys, specs):
        store.put(key, running_shard(spec))
    first = client.latest('owned-session')
    client.update_control('owned-session', control(clock_epoch=1))
    assert client.latest('owned-session') is None
    store.put(owned.score_keys[0], running_shard(specs[0], epoch=1))
    assert client.latest('owned-session') is None
    store.put(owned.score_keys[1], running_shard(specs[1], epoch=1))
    updated = client.latest('owned-session')
    assert updated['clock_epoch'] == 1 and updated['worker_sequence'] > first['worker_sequence']


def test_out_of_order_shard_sequence_and_cross_shard_match_records_are_rejected(monkeypatch):
    client, _, store, specs, owned = started_shards(monkeypatch)
    for key, spec in zip(owned.score_keys, specs):
        store.put(key, running_shard(spec, sequence=3))
    assert client.latest('owned-session')
    store.put(owned.score_keys[0], running_shard(specs[0], sequence=2))
    store.put(owned.score_keys[1], running_shard(specs[1], sequence=4))
    assert client.latest('owned-session') is None
    invalid = running_shard(specs[0], sequence=4)
    invalid['scores'][0]['match_id'] = 'cam-7'
    store.put(owned.score_keys[0], invalid)
    assert client.latest('owned-session') is None


def test_error_shard_fails_fast_even_if_other_shard_never_published(monkeypatch):
    client, _, store, specs, owned = started_shards(monkeypatch)
    store.put(owned.score_keys[1], running_shard(specs[1], state='error'))
    error = client.latest('owned-session')
    assert error['state'] == 'error' and error['reason'] == 'shard_error'


@pytest.mark.parametrize('reason,result', [('paused', 'shard_stopped'), ('session_limit', 'session_limit')])
def test_non_eof_shard_exit_stops_the_group_for_bounded_backend_recovery(monkeypatch, reason, result):
    client, _, store, specs, owned = started_shards(monkeypatch)
    store.put(owned.score_keys[0], running_shard(specs[0], state='stopped', reason=reason))
    store.put(owned.score_keys[1], running_shard(specs[1]))
    merged = client.latest('owned-session')
    assert merged['state'] == 'stopped' and merged['reason'] == result


def test_ended_shard_removes_its_scores_and_continues_remaining_healthy_sources(monkeypatch):
    client, _, store, specs, owned = started_shards(monkeypatch)
    store.put(owned.score_keys[0], running_shard(specs[0], state='stopped', reason='all_sources_ended'))
    store.put(owned.score_keys[1], running_shard(specs[1]))
    merged = client.latest('owned-session')
    assert merged['state'] == 'running' and len(merged['scores']) == 3
    assert all(merged['health'][f'cam-{i}']['status'] == 'eof' for i in range(1, 5))
    store.put(owned.score_keys[1], running_shard(specs[1], state='stopped', sequence=2, reason='all_sources_ended'))
    ended = client.latest('owned-session')
    assert ended['state'] == 'stopped' and ended['reason'] == 'all_sources_ended' and not ended['scores']


def test_unstopped_owned_session_cannot_spawn_another_unbounded_worker_group(monkeypatch):
    client, sdk, _, _, _ = started_shards(monkeypatch)
    with pytest.raises(RuntimeError, match='already owns'):
        client.start(multi_session())
    assert len(sdk.handles) == 2
