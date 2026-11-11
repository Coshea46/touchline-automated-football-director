"""Reusable synthetic test doubles and builders; no provider calls."""


import copy
from types import SimpleNamespace
from touchline.ml_inference.streaming import client as replay_client
from touchline.ml_inference.streaming.client import ReplayClient


def session(**extra):
    return {
        "session_id": "owned-session", "mode": "features_mock",
        "model_version": "features-mock-v1", "max_duration_seconds": 20,
        "max_estimated_cost_usd": 1,
        "matches": [{"match_id": "cam-1", "source_url": "https://media.example/video.mp4?secret=private",
                     "offset_seconds": 0}], **extra,
    }


def control(**extra):
    return {"position": 0, "playing": False, "active": True,
            "expires_at": 1005, "clock_epoch": 0, **extra}


class MemoryStore:
    def __init__(self):
        self.values, self.events = {}, []

    def get(self, key, default=None):
        return copy.deepcopy(self.values.get(key, default))

    def put(self, key, value):
        self.events.append(("put", key, copy.deepcopy(value)))
        self.values[key] = copy.deepcopy(value)

    def pop(self, key, *default):
        self.events.append(("pop", key))
        return self.values.pop(key, *default)


def sdk_fixture(monkeypatch):
    store, lookups, submissions = MemoryStore(), [], []

    class Handle:
        def __init__(self, index=0):
            self.index = index

        def cancel(self, **kwargs):
            store.events.append(("cancel", kwargs, copy.deepcopy(store.values)))
            sdk.cancelled.append(self.index)
            if self.index in sdk.cancel_fail:
                raise TimeoutError('Fixture cancellation failure')

    handle = Handle()

    class Dict:
        @staticmethod
        def from_name(name, **kwargs):
            assert name == "matchpulse-inference-replay"
            assert kwargs == {"create_if_missing": True}
            return store

    class Cls:
        @staticmethod
        def from_name(app, name):
            lookups.append((app, name))

            def spawn(spec):
                submissions.append(copy.deepcopy(spec))
                index = len(submissions) - 1
                if index == sdk.fail_at:
                    raise RuntimeError('Fixture submission failure')
                owned = handle if index == 0 else Handle(index)
                sdk.handles.append(owned)
                return owned

            return lambda: SimpleNamespace(run=SimpleNamespace(spawn=spawn))

    monkeypatch.setattr(replay_client, "time", SimpleNamespace(time=lambda: 1000))
    sdk = SimpleNamespace(Dict=Dict, Cls=Cls, handles=[], cancelled=[], cancel_fail=set(), fail_at=None)
    return sdk, store, lookups, submissions, handle


def multi_session(count=7, **extra):
    return session(matches=[{'match_id': f'cam-{i}', 'source_url': f'https://media.example/{i}.mp4',
                             'offset_seconds': i * 10} for i in range(1, count + 1)], **extra)


def running_shard(spec, *, sequence=1, epoch=0, state='running', reason=None, end=5):
    ids = [match['match_id'] for match in spec['matches']]
    records = [{'session_id': spec['session_id'], 'match_id': key,
                'model_version': spec['model_version'], 'clock_epoch': epoch, 'sequence': sequence,
                'observation_start': end - 5, 'observation_end': end, 'produced_at': 1000,
                'score': .8 if key == 'cam-7' else .1, 'latency_seconds': .2, 'status': 'healthy'} for key in ids]
    return {'session_id': spec['session_id'], 'model_version': spec['model_version'], 'clock_epoch': epoch,
            'worker_sequence': sequence, 'score_kind': 'mock', 'state': state, 'reason': reason,
            'produced_at': 1000, 'scores': records if state == 'running' else [],
            'health': {key: {'status': 'healthy'} for key in ids},
            'metrics': {'detector_frames': 75 * len(ids), 'detector_seconds': len(ids) * .1,
                        'peak_rss_bytes': len(ids) * 1000, 'elapsed_seconds': 6,
                        'runtime': {'container_id': 'worker-' + str(spec.get('shard_id', 'single'))},
                        'streams': {key: {} for key in ids}}}


def started_shards(monkeypatch, count=7, **extra):
    sdk, store, _lookups, specs, _ = sdk_fixture(monkeypatch)
    client = ReplayClient(sdk=sdk)
    client.update_control('owned-session', control())
    # Existing merge/race regressions deliberately exercise the two-worker
    # diagnostic partition. The auto-capacity policy is tested separately.
    extra.setdefault('gpu_workers', 2 if count > 1 else 1)
    owned = client.start(multi_session(count, **extra))
    return client, sdk, store, specs, owned
