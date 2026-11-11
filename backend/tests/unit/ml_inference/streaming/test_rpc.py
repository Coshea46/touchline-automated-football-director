"""Offline tests for unit ml_inference streaming test_rpc."""


import asyncio
import pytest
from touchline.ml_inference.streaming import client as replay_client
from touchline.ml_inference.streaming.client import ReplayClient
from tests.support.ml_inference.client import session, control, sdk_fixture, running_shard


def test_public_sdk_rpc_deadline_cancels_the_underlying_async_operation():
    reached, cancelled = [], []

    class BlockingRPC:
        def __call__(self):
            raise AssertionError('Synchronous SDK path must not be used')

        async def aio(self):
            reached.append(True)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

    with pytest.raises(TimeoutError):
        replay_client._rpc(BlockingRPC(), timeout=.01)
    assert reached == [True] and cancelled == [True]


def test_every_production_rpc_uses_public_aio_while_plain_fakes_remain_supported(monkeypatch):
    sdk, store, _, _, _ = sdk_fixture(monkeypatch)
    calls = []

    class AsyncRPC:
        def __init__(self, name, function):
            self.name, self.function = name, function

        def __call__(self, *args, **kwargs):
            raise AssertionError('Synchronous SDK wrapper must not be called')

        async def aio(self, *args, **kwargs):
            calls.append(self.name)
            return self.function(*args, **kwargs)

    for name in ('get', 'put', 'pop'):
        setattr(store, name, AsyncRPC(name, getattr(store, name)))
    original_lookup = sdk.Cls.from_name

    def lookup(*args):
        worker = original_lookup(*args)()
        original_spawn = worker.run.spawn

        def spawn(spec):
            handle = original_spawn(spec)
            handle.cancel = AsyncRPC('cancel', handle.cancel)
            return handle

        worker.run.spawn = AsyncRPC('spawn', spawn)
        return lambda: worker

    sdk.Cls.from_name = lookup
    client = ReplayClient(sdk=sdk)
    client.update_control('owned-session', control())
    owned = client.start(session())
    store.values[owned.score_keys[0]] = running_shard(session())
    assert client.latest('owned-session')['state'] == 'running'
    client.stop('owned-session', owned)
    assert {'get', 'put', 'pop', 'spawn', 'cancel'} <= set(calls)
    assert not store.values
