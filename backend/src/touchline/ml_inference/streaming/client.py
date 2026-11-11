"""Lazy compact-state bridge with a bounded owned GPU group and latest scores."""

import asyncio
import copy
import importlib
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock, RLock

from touchline.ml_inference.config import APP_NAME
from touchline.ml_inference.streaming.contract import (
    MATCHES_PER_GPU,
    MAX_GPU_WORKERS,
    MAX_WORKER_BUDGET_USD,
    REPLAY_DICT,
    identifier,
    validate_control,
    validate_session,
)


def _rpc(method, *args, timeout=5, **kwargs):
    """Cancel public SDK async calls at a real deadline inside the CPU thread.

    Ordinary injected test methods stay synchronous. Production Modal methods
    expose .aio; wait_for cancellation reaches their RPC rather than merely
    abandoning an indefinitely retrying synchronous default-executor thread.
    """
    asynchronous = getattr(method, 'aio', None)
    if not callable(asynchronous):
        return method(*args, **kwargs)

    async def invoke():
        return await asyncio.wait_for(asynchronous(*args, **kwargs), timeout=timeout)

    return asyncio.run(invoke())


@dataclass(frozen=True)
class OwnedReplayHandles:
    session_id: str
    handles: tuple
    score_keys: tuple

    def cancel(self, **kwargs):
        failures = []
        for handle in self.handles:
            try:
                _rpc(handle.cancel, timeout=10, **kwargs)
            except Exception as exc:  # noqa: BLE001 - attempt every owned cancellation, publish classes only.
                failures.append(type(exc).__name__)
        if failures:
            raise RuntimeError('Owned replay cancellation failed (' + ', '.join(failures) + ')') from None


@dataclass
class _Session:
    session_id: str
    model_version: str
    score_kind: str
    epoch: int
    keys: tuple
    match_groups: tuple
    signature: object = None
    sequence: int = 0
    cached: object = None
    shard_sequences: tuple = field(default_factory=tuple)


class ReplayClient:
    def __init__(self, *, sdk=None):
        self.sdk = sdk
        self._dictionary = None
        self._active = None
        self._lock = RLock()
        self._read_lock = Lock()

    def _store(self):
        with self._lock:
            if self._dictionary is None:
                self.sdk = self.sdk or importlib.import_module('modal')
                self._dictionary = self.sdk.Dict.from_name(REPLAY_DICT, create_if_missing=True)
            return self._dictionary

    def update_control(self, session_id, control):
        validate_control(session_id, control)
        _rpc(self._store().put, 'control:' + session_id, {**control, 'session_id': session_id}, timeout=3)
        with self._lock:
            active = self._active
            if active is not None and active.session_id == session_id and active.epoch != control.get('clock_epoch', 0):
                active.epoch = control.get('clock_epoch', 0)
                active.signature, active.cached, active.shard_sequences = None, None, ()

    def start(self, session_spec):
        validate_session(session_spec)
        session_id = session_spec['session_id']
        store = self._store()
        clock = _rpc(store.get, 'control:' + session_id, None, timeout=3)
        if not clock or not clock.get('active') or clock.get('expires_at', 0) <= time.time():
            raise RuntimeError('A fresh viewer clock/lease is required before monitoring')
        validate_control(session_id, clock)
        matches = session_spec['matches']
        count = session_spec.get('gpu_workers', math.ceil(len(matches) / MATCHES_PER_GPU))
        size, remainder = divmod(len(matches), count)
        groups, offset = [], 0
        for index in range(count):
            width = size + int(index < remainder)
            groups.append(matches[offset:offset + width])
            offset += width
        groups = tuple(groups)
        keys = tuple('scores:' + session_id + (':' + str(index) if count > 1 else '') for index in range(count))
        active = _Session(session_id, session_spec['model_version'],
                          'model' if session_spec['mode'] == 'real' else 'mock',
                          clock.get('clock_epoch', 0), keys,
                          tuple(frozenset(match['match_id'] for match in group) for group in groups))
        with self._lock:
            if self._active is not None:
                raise RuntimeError('Replay client already owns an active session; stop it first')
            self._active = active
        handles = []
        try:
            for key in keys:
                _rpc(store.pop, key, None, timeout=3)
            # Parameters would create independent autoscaling pools. Submit both
            # long inputs to the one fixed, bounded Modal class pool instead.
            worker = self.sdk.Cls.from_name(APP_NAME, 'ReplayWorker')()
            for index, group in enumerate(groups):
                spec = dict(session_spec)
                # Deployed workers retain their original per-input ceiling.
                # A larger parent allowance must not require a cloud redeploy.
                spec['max_estimated_cost_usd'] = min(
                    session_spec.get('max_estimated_cost_usd', 1) / count, MAX_WORKER_BUDGET_USD)
                if count > 1:
                    spec.update(matches=group, gpu_workers=1, shard_id=str(index))
                    if 'mock_scores' in spec:
                        spec['mock_scores'] = {key: score for key, score in spec['mock_scores'].items()
                                               if key in active.match_groups[index]}
                validate_session(spec)
                handles.append(_rpc(worker.run.spawn, spec, timeout=15))
        except Exception:  # Roll back only inputs owned by this failed submission.
            try:
                self.stop(session_id, OwnedReplayHandles(session_id, tuple(handles), keys))
            except Exception:  # noqa: BLE001, S110 - preserve submission error after bounded owned cleanup; lease expires.
                pass
            raise
        return OwnedReplayHandles(session_id, tuple(handles), keys)

    @staticmethod
    def _valid_snapshot(snapshot, active, index):
        if (not isinstance(snapshot, dict) or snapshot.get('session_id') != active.session_id
                or snapshot.get('model_version') != active.model_version
                or type(snapshot.get('clock_epoch')) is not int
                or snapshot.get('clock_epoch') != active.epoch
                or snapshot.get('score_kind') != active.score_kind
                or type(snapshot.get('worker_sequence')) is not int or snapshot['worker_sequence'] < 0
                or snapshot.get('state') not in ('starting', 'ready', 'running', 'error', 'stopped')):
            return False
        ids = active.match_groups[index]
        records, health = snapshot.get('scores'), snapshot.get('health')
        if (not isinstance(records, list) or len(records) > len(ids) or not isinstance(health, dict) or set(health) - ids
                or not isinstance(snapshot.get('metrics', {}), dict)
                or isinstance(snapshot.get('produced_at'), bool)
                or not isinstance(snapshot.get('produced_at'), (int, float))
                or not math.isfinite(snapshot['produced_at'])):
            return False
        seen = set()
        for record in records:
            if (not isinstance(record, dict) or not isinstance(record.get('match_id'), str) or record.get('match_id') not in ids
                    or record['match_id'] in seen or record.get('session_id') != active.session_id
                    or record.get('model_version') != active.model_version or record.get('clock_epoch') != active.epoch):
                return False
            seen.add(record['match_id'])
        return True

    @staticmethod
    def _metrics(snapshots):
        shards = {str(index): item.get('metrics', {}) for index, item in enumerate(snapshots) if item is not None}
        summed = ('detector_seconds', 'detector_frames', 'detector_batches', 'window_assembly_seconds',
                  'classifier_seconds', 'publication_seconds', 'deadline_misses', 'windows',
                  'decoder_failures', 'estimated_compute_usd', 'visual_append_seconds',
                  'visual_wait_seconds', 'visual_jobs', 'visual_failures',
                  'window_assembly_task_seconds', 'window_assembly_failures')
        result = {key: sum(value.get(key, 0) for value in shards.values()) for key in summed}
        result.update({key: max((value.get(key, 0) for value in shards.values()), default=0)
                       for key in ('elapsed_seconds', 'max_queue_samples', 'max_visual_pending', 'max_assembly_pending')})
        memory = [value.get('peak_rss_bytes') for value in shards.values()
                  if isinstance(value.get('peak_rss_bytes'), (int, float))]
        result['peak_rss_bytes'] = max(memory, default=None)
        result['total_peak_rss_bytes'] = sum(memory) if memory else None
        result['runtime'] = {'gpu_workers': len(snapshots),
                             'workers': [value.get('runtime', {}) for value in shards.values()]}
        result['streams'] = {key: value for shard in shards.values()
                             for key, value in shard.get('streams', {}).items()}
        result['source_failure_types'] = {}
        for shard in shards.values():
            for key, value in shard.get('source_failure_types', {}).items():
                result['source_failure_types'][key] = result['source_failure_types'].get(key, 0) + value
        channels = [shard.get('state_io', {}) for shard in shards.values()]
        result['state_io'] = {key: sum(channel.get(key, 0) for channel in channels)
                              for key in ('control_reads', 'control_seconds', 'control_errors', 'publications',
                                          'publication_seconds', 'publication_errors', 'coalesced_snapshots')}
        resources = [channel.get('resources', {}) for channel in channels]
        gpu_samples = sum(value.get('gpu_samples', 0) for value in resources)
        result['state_io']['resources'] = {
            'gpu_samples': gpu_samples,
            'gpu_query_errors': sum(value.get('gpu_query_errors', 0) for value in resources),
            'gpu_utilization_mean_percent': (sum((value.get('gpu_utilization_mean_percent') or 0)
                                                 * value.get('gpu_samples', 0) for value in resources) / gpu_samples
                                             if gpu_samples else None),
            'gpu_utilization_max_percent': max((value['gpu_utilization_max_percent'] for value in resources
                                                if value.get('gpu_utilization_max_percent') is not None), default=None),
            'gpu_memory_max_mib': max((value['gpu_memory_max_mib'] for value in resources
                                      if value.get('gpu_memory_max_mib') is not None), default=None),
        }
        result['shards'] = shards
        return result

    def _merge(self, snapshots, active):
        valid = [item if self._valid_snapshot(item, active, index) else None
                 for index, item in enumerate(snapshots)]
        failed = any(item is not None and item['state'] == 'error' for item in valid)
        if not failed and any(item is None for item in valid):
            return None
        sequences = tuple(item['worker_sequence'] if item is not None else None for item in valid)
        if active.shard_sequences and any(old is not None and new is not None and new < old
                                          for old, new in zip(active.shard_sequences, sequences)):
            return None
        signature = (active.epoch, sequences)
        if signature == active.signature:
            return copy.deepcopy(active.cached)
        present = [item for item in valid if item is not None]
        eof = [item['state'] == 'stopped' and item.get('reason') == 'all_sources_ended' for item in present]
        reason = None
        if failed:
            state, reason = 'error', 'shard_error'
        elif any(item['state'] == 'stopped' and item.get('reason') == 'session_limit' for item in present):
            state, reason = 'stopped', 'session_limit'
        elif all(eof):
            state, reason = 'stopped', 'all_sources_ended'
        elif any(item['state'] == 'stopped' and not ended for item, ended in zip(present, eof)):
            state, reason = 'stopped', 'shard_stopped'
        elif any(item['state'] == 'starting' for item in present):
            state = 'starting'
        else:
            state = 'running' if any(item['state'] == 'running' for item in present) else 'ready'
        health, records = {}, []
        for index, item in enumerate(valid):
            if item is None:
                continue
            if item['state'] == 'stopped' and item.get('reason') == 'all_sources_ended':
                health.update({match_id: {'status': 'eof'} for match_id in active.match_groups[index]})
            else:
                health.update(item['health'])
                records.extend(item['scores'])
        active.sequence += 1
        active.signature, active.shard_sequences = signature, sequences
        active.cached = {'session_id': active.session_id, 'model_version': active.model_version,
                         'clock_epoch': active.epoch, 'worker_sequence': active.sequence,
                         'score_kind': active.score_kind, 'state': state, 'reason': reason,
                         'produced_at': max(item.get('produced_at', 0) for item in present),
                         'scores': records, 'health': health, 'metrics': self._metrics(valid)}
        if failed:
            active.cached['shard_errors'] = {
                str(index): {key: item[key] for key in ('reason', 'error_type', 'error_phase')
                    if isinstance(item.get(key), str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', item[key])}
                for index, item in enumerate(valid) if item is not None and item['state'] == 'error'}
        return copy.deepcopy(active.cached)

    def latest(self, session_id):
        identifier(session_id, 'session_id')
        # One poll at a time, at most one read per bounded physical worker.
        with self._read_lock:
            store = self._store()
            with self._lock:
                active = self._active
                if active is not None and active.session_id != session_id:
                    raise ValueError('Client does not own this replay session')
                if active is None:
                    return _rpc(store.get, 'scores:' + session_id, None, timeout=3)
                keys = active.keys
            if len(keys) == 1:
                snapshots = [_rpc(store.get, keys[0], None, timeout=3)]
            else:
                with ThreadPoolExecutor(max_workers=min(MAX_GPU_WORKERS, len(keys)), thread_name_prefix='replay-score') as pool:
                    snapshots = list(pool.map(lambda key: _rpc(store.get, key, None, timeout=3), keys))
            with self._lock:
                if self._active is not active:
                    return None
                if len(keys) == 1:
                    return snapshots[0] if self._valid_snapshot(snapshots[0], active, 0) else None
                return self._merge(snapshots, active)

    def stop(self, session_id, handle):
        identifier(session_id, 'session_id')
        if isinstance(handle, OwnedReplayHandles) and handle.session_id != session_id:
            raise ValueError('Replay handles belong to a different session')
        store = self._store()
        with self._lock:
            active = self._active
            keys = (handle.score_keys if isinstance(handle, OwnedReplayHandles) else
                    active.keys if active is not None and active.session_id == session_id else ('scores:' + session_id,))
        try:
            _rpc(store.put, 'control:' + session_id, {'session_id': session_id, 'active': False,
                 'playing': False, 'position': 0, 'clock_epoch': 0, 'expires_at': time.time()}, timeout=3)
        finally:
            try:
                if handle is not None:
                    _rpc(handle.cancel, terminate_containers=True, timeout=10)
            finally:
                cleanup_errors = []
                for key in ('control:' + session_id, *keys):
                    try:
                        _rpc(store.pop, key, None, timeout=3)
                    except Exception as exc:  # noqa: BLE001 - clean every owned key even after a storage failure.
                        cleanup_errors.append(type(exc).__name__)
                with self._lock:
                    if self._active is not None and self._active.session_id == session_id:
                        self._active = None
                if cleanup_errors:
                    raise RuntimeError('Replay state cleanup failed (' + ', '.join(cleanup_errors) + ')') from None
