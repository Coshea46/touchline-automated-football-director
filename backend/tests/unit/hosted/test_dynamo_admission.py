"""Public admission concurrency and historical preservation, without AWS calls."""

import asyncio
from copy import deepcopy
from decimal import Decimal
import threading

import pytest

from touchline.hosted.admission import AdmissionDenied
from touchline.hosted.dynamo_admission import DynamoAdmissionPolicy, MAX_COLLISION_ATTEMPTS
from tests.support.dynamo import Dynamo


FUNDED = {"TOUCHLINE_AI_DAILY_BUDGET_USD": "100", "TOUCHLINE_AI_TOTAL_BUDGET_USD": "100"}


def test_daily_usage_ignores_retired_lifetime_cap_settles_once_and_retains_history():
    async def case():
        db = Dynamo()
        clock = [10000]
        admission = policy(db, env={"TOUCHLINE_AI_BUDGET_MODE":"daily_usage",
            "TOUCHLINE_AI_DAILY_BUDGET_USD":"8", "TOUCHLINE_AI_TOTAL_BUDGET_USD":"0"}, now=lambda:clock[0])
        leases = await asyncio.gather(*(admission.acquire_ai(str(i), str(i)) for i in range(3)))
        with pytest.raises(AdmissionDenied, match="ai_daily_limit"):
            await admission.acquire_ai('fourth','fourth')
        before = deepcopy(db.items)
        permit = leases[0]['permit_id']
        await admission.release_ai(permit, confirmed=False)
        assert db.items == before
        clock[0] += 60
        await asyncio.gather(*(admission.release_ai(permit, confirmed=True) for _ in range(2)))
        record = db.items[('permit#'+permit,'record')]
        assert record['amount_microusd'] == 2600000
        assert 0 < record['charged_microusd'] < 2600000
        assert db.items[('budget','usage-open')]['amount_microusd'] == 5200000
        settled = deepcopy(db.items)
        await admission.release_ai(permit, confirmed=True)
        assert db.items == settled
        await admission.release_ai(leases[1]['permit_id'], confirmed=True)
        await admission.acquire_ai('fourth','fourth')
        assert db.items[('budget','total')]['amount_microusd'] >= before[('budget','total')]['amount_microusd']
    asyncio.run(case())


def test_daily_reset_carries_unclosed_reservations_and_does_not_refund_legacy_permits():
    async def case():
        db = Dynamo()
        db.historical('legacy')
        clock = [86300]
        admission = policy(db, env={**FUNDED, 'TOUCHLINE_AI_BUDGET_MODE':'daily_usage',
            'TOUCHLINE_AI_DAILY_BUDGET_USD':'8'}, now=lambda:clock[0])
        leases = await asyncio.gather(*(admission.acquire_ai(str(i),str(i)) for i in range(3)))
        clock[0] = 86410
        with pytest.raises(AdmissionDenied, match='ai_daily_limit'):
            await admission.acquire_ai('new','new')
        for lease in leases:
            await admission.release_ai(lease['permit_id'], confirmed=True)
        await admission.acquire_ai('new','new')
        old_daily = deepcopy(db.items[('budget','day#1970-01-01')]) if ('budget','day#1970-01-01') in db.items else None
        await admission.release_ai('legacy', confirmed=True)
        assert db.items.get(('budget','day#1970-01-01')) == old_daily
        assert 2600000 < db.items[('budget','usage-day#1970-01-02')]['amount_microusd'] < 8000000
    asyncio.run(case())


def policy(db, *, env=None, now=lambda: 10000):
    return DynamoAdmissionPolicy("offline-admission", client=db,
        env=FUNDED if env is None else env, now=now, monotonic=lambda: 1000)


def test_lazy_constructor_never_creates_sqlite_or_client_and_warm_requires_complete_migration():
    async def case():
        called = []
        db = Dynamo()
        admission = DynamoAdmissionPolicy("offline-admission", client_factory=lambda: called.append(1) or db, env={})
        assert called == [] and not hasattr(admission, "_path")
        db.items[("config", "settings")]["migration_ready"] = False
        with pytest.raises(AdmissionDenied, match="admission_storage_unavailable"):
            await admission.warm()
        assert admission._salt is None and called == [1]
        db.items[("config", "settings")]["migration_ready"] = True
        await admission.warm()
        assert admission._salt == "synthetic-preserved-peer-salt"
        assert admission.daily_budget == 1.2 and admission.total_budget == 3
        await admission.close()
        assert db.closed
    asyncio.run(case())


def test_parallel_budget_race_across_policy_instances_grants_exactly_one_without_lost_spend():
    async def case():
        db, barrier = Dynamo(), threading.Barrier(2)
        count, lock = [0], threading.Lock()
        def gate():
            with lock:
                count[0] += 1
                initial = count[0] <= 2
            if initial:
                barrier.wait(2)
        db.before_write = gate
        env = {"TOUCHLINE_AI_DAILY_BUDGET_USD": "2.60", "TOUCHLINE_AI_TOTAL_BUDGET_USD": "2.60"}
        first, second = policy(db, env=env), policy(db, env=env)
        results = await asyncio.gather(first.acquire_ai("a", "peer-a"), second.acquire_ai("b", "peer-b"),
                                       return_exceptions=True)
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, AdmissionDenied) and result.code == "ai_budget_exhausted" for result in results) == 1
        assert len(db.permits()) == 1
        assert db.items[("budget", "total")]["amount_microusd"] == 2_600_000
    asyncio.run(case())


def test_tenth_ip_start_race_counts_historical_components_and_never_changes_old_records():
    async def case():
        db = Dynamo()
        for index in range(9):
            db.historical(f"old-{index}", visitor=f"old-{index}", peer="peer",
                component="director" if index % 2 else "captions", closed=True)
        before = deepcopy(db.permits())
        results = await asyncio.gather(policy(db).acquire_ai("new-a", "peer"),
            policy(db).acquire_ai("new-b", "peer", component="captions"), return_exceptions=True)
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, AdmissionDenied) and result.code == "ai_rate_limited" for result in results) == 1
        assert db.permits()[:9] == before
        assert len(db.permits()) == 10
        await policy(db).acquire_ai("other-visitor", "other-peer")
    asyncio.run(case())


def test_uncertain_legacy_blocks_both_components_only_for_its_visitor_until_confirmed_release():
    async def case():
        db = Dynamo()
        db.historical("old-combined")
        admission = policy(db)
        for component in ("director", "captions"):
            with pytest.raises(AdmissionDenied, match="ai_cleanup_pending"):
                await admission.acquire_ai("old-visitor", "old-peer", component=component)
        await admission.acquire_ai("other", "other-peer")
        await admission.release_ai("old-combined", confirmed=False)
        assert db.items[("permit#old-combined", "record")]["closed"] is False
        spent = db.items[("budget", "total")]["amount_microusd"]
        await admission.release_ai("old-combined", confirmed=True)
        assert db.items[("budget", "total")]["amount_microusd"] == spent
        assert all(db.items[("visitor#old-visitor", "owner#" + component)]["closed"]
                   for component in ("director", "captions"))
        await admission.acquire_ai("old-visitor", "old-peer")
        await admission.acquire_ai("old-visitor", "old-peer", component="captions")
    asyncio.run(case())


def test_release_is_idempotent_and_cannot_close_new_owner_or_another_component():
    async def case():
        db, admission = Dynamo(), None
        admission = policy(db)
        first = await admission.acquire_ai("visitor", "peer")
        captions = await admission.acquire_ai("visitor", "peer", component="captions")
        await admission.release_ai(first["permit_id"], confirmed=True)
        second = await admission.acquire_ai("visitor", "peer")
        before = deepcopy(db.items)
        await admission.release_ai(first["permit_id"], confirmed=True)
        assert db.items == before
        assert db.items[("visitor#visitor", "owner#director")]["permit_id"] == second["permit_id"]
        assert db.items[("permit#" + captions["permit_id"], "record")]["closed"] is False
        assert db.items[("budget", "total")]["amount_microusd"] == 5_340_000
    asyncio.run(case())


def test_ambiguous_committed_start_never_grants_or_retries_and_retains_budget_and_owner():
    async def case():
        db = Dynamo()
        db.fail_after_write = True
        with pytest.raises(AdmissionDenied) as caught:
            await policy(db).acquire_ai("visitor", "peer")
        assert str(caught.value) == "admission_storage_unavailable"
        assert "PRIVATE" not in str(caught.value)
        assert len(db.tokens) == len(db.transactions) == len(db.permits()) == 1
        db.fail_after_write = False
        with pytest.raises(AdmissionDenied, match="ai_busy"):
            await policy(db).acquire_ai("visitor", "peer")
        await policy(db).acquire_ai("unrelated", "other-peer")
        assert db.items[("budget", "total")]["amount_microusd"] == 5_200_000
    asyncio.run(case())


def test_only_definite_conditional_collisions_retry_with_bounded_fresh_transaction_tokens():
    async def case():
        db = Dynamo()
        db.collisions = 2
        lease = await policy(db).acquire_ai("visitor", "peer")
        assert lease["duration_seconds"] == 1200 and lease["expires_at"] == 11200
        assert len(db.tokens) == len(set(db.tokens)) == 3
        assert len(db.permits()) == 1
        exhausted = Dynamo()
        exhausted.collisions = 100
        with pytest.raises(AdmissionDenied, match="admission_storage_unavailable"):
            await policy(exhausted).acquire_ai("visitor", "peer")
        assert len(exhausted.tokens) == MAX_COLLISION_ATTEMPTS and not exhausted.permits()
    asyncio.run(case())


def test_creation_limits_and_peer_salt_survive_policy_replacement_without_raw_ip_storage():
    async def case():
        db = Dynamo()
        first = policy(db)
        key = await first.admit_new("192.0.2.10")
        await first.admit_new("192.0.2.10")
        await first.admit_new("192.0.2.10")
        restored = policy(db)
        with pytest.raises(AdmissionDenied, match="session_rate_limited") as caught:
            await restored.admit_new("192.0.2.10")
        assert caught.value.retry_after == 600
        assert len(key) == 64 and key == restored.peer_key("192.0.2.10")
        assert "192.0.2.10" not in repr(db.items)
        for index in range(17):
            await first.admit_new(f"other-{index}")
        with pytest.raises(AdmissionDenied, match="session_rate_limited"):
            await first.admit_new("new-peer")
        await policy(db, now=lambda: 10600).admit_new("192.0.2.10")
    asyncio.run(case())


def test_durable_write_wait_and_cancellation_do_not_starve_local_heartbeats_or_grant_paid_work():
    async def case():
        db, started, release = Dynamo(), threading.Event(), threading.Event()
        def gate():
            started.set()
            assert release.wait(2)
        db.before_write = gate
        admission = policy(db)
        task = asyncio.create_task(admission.acquire_ai("visitor", "peer"))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            await asyncio.wait_for(asyncio.gather(admission.allow_message("existing"),
                admission.allow_connection("other-peer"), admission.allow_http("other-peer")), .2)
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(db.permits()) == 1
        with pytest.raises(AdmissionDenied, match="ai_busy"):
            await admission.acquire_ai("visitor", "peer")
    asyncio.run(case())


def test_counters_enforce_exact_historical_nonrefundable_amounts_and_new_utc_day():
    async def case():
        db = Dynamo()
        db.historical("old", amount=Decimal(".70"), closed=True)
        admission = policy(db, env={"TOUCHLINE_AI_DAILY_BUDGET_USD": "3.20", "TOUCHLINE_AI_TOTAL_BUDGET_USD": "3.30"})
        with pytest.raises(AdmissionDenied, match="ai_daily_limit"):
            await admission.acquire_ai("new", "new-peer")
        tomorrow = policy(db, env={"TOUCHLINE_AI_DAILY_BUDGET_USD": "3.20", "TOUCHLINE_AI_TOTAL_BUDGET_USD": "3.30"}, now=lambda: 90000)
        lease = await tomorrow.acquire_ai("new", "new-peer")
        await tomorrow.release_ai(lease["permit_id"], confirmed=True)
        with pytest.raises(AdmissionDenied, match="ai_budget_exhausted"):
            await tomorrow.acquire_ai("caption", "new-peer", component="captions")
        assert db.items[("budget", "total")]["amount_microusd"] == 3_300_000
        assert db.items[("budget", "day#1970-01-01")]["amount_microusd"] == 700_000
        assert db.items[("budget", "day#1970-01-02")]["amount_microusd"] == 2_600_000
    asyncio.run(case())


def test_concurrent_last_creation_slot_is_atomic_for_peer_and_global_vectors():
    async def case():
        db = Dynamo()
        first, second = policy(db), policy(db)
        await first.warm()
        key = first.peer_key("same-peer")
        db.seed("creations", "global", events=[Decimal("9990")] * 19, version=0)
        db.seed("creations", "peer#" + key, events=[Decimal("9990")] * 2, version=0)
        results = await asyncio.gather(first.admit_new("same-peer"), second.admit_new("same-peer"),
                                       return_exceptions=True)
        assert sum(isinstance(result, str) for result in results) == 1
        assert sum(isinstance(result, AdmissionDenied) and result.code == "session_rate_limited" for result in results) == 1
        assert len(db.items[("creations", "global")]["events"]) == 20
        assert len(db.items[("creations", "peer#" + key)]["events"]) == 3
    asyncio.run(case())


def test_shutdown_waits_for_native_transaction_and_closes_sdk_client_once():
    async def case():
        db, started, release = Dynamo(), threading.Event(), threading.Event()
        close_calls = []
        def gate():
            started.set()
            assert release.wait(2)
        def close():
            assert release.is_set()
            close_calls.append(1)
        db.before_write, db.close = gate, close
        admission = policy(db)
        start = asyncio.create_task(admission.acquire_ai("visitor", "peer"))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            closing = asyncio.create_task(admission.close())
            await asyncio.sleep(.01)
            assert not closing.done() and not close_calls
        finally:
            release.set()
        await asyncio.gather(start, closing)
        await admission.close()
        assert close_calls == [1]
    asyncio.run(case())


def test_default_oidc_provider_and_dedicated_client_are_lazy_and_owned_once(monkeypatch):
    async def case():
        db, constructed, closed, client_calls = Dynamo(), [], [], []
        class Provider:
            region = "eu-west-2"
            def __init__(self, env):
                constructed.append(env)
            def _get_session(self):
                return self
            def client(self, service, **kwargs):
                client_calls.append((service, kwargs))
                return db
            def close(self):
                assert db.closed
                closed.append(1)
        monkeypatch.setattr("touchline.storage.S3Access", Provider)
        env = {"TOUCHLINE_ADMISSION_DYNAMODB_TABLE": "offline-admission"}
        admission = DynamoAdmissionPolicy(env=env)
        assert constructed == []
        await admission.warm()
        assert constructed == [env] and len(client_calls) == 1
        service, options = client_calls[0]
        assert service == "dynamodb" and options["region_name"] == "eu-west-2"
        assert options["config"].retries["max_attempts"] == 0
        assert not any(name.startswith("aws_") for name in options)
        await admission.close()
        await admission.close()
        assert closed == [1]
    asyncio.run(case())


def test_sdk_validates_transaction_shapes_offline_and_ambiguous_error_fields_never_escape():
    async def case():
        from botocore.session import get_session
        from botocore.validate import validate_parameters
        from botocore.exceptions import ClientError
        shape = get_session().get_service_model("dynamodb").operation_model("TransactWriteItems").input_shape
        db = Dynamo()
        await policy(db).acquire_ai("visitor", "peer")
        for request in db.transactions:
            validate_parameters(request, shape)
        for response in ({"Error": None}, {"Error": {"Code": "TransactionCanceledException"},
                "CancellationReasons": [None]}, {"Error": {"Code": "AccessDeniedException",
                "Message": "PRIVATE_SIGNED_URL_TOKEN"}}):
            bad = Dynamo()
            def write(**_):
                raise ClientError(response, "TransactWriteItems")
            bad.transact_write_items = write
            with pytest.raises(AdmissionDenied) as caught:
                await policy(bad).acquire_ai("visitor", "peer")
            assert str(caught.value) == "admission_storage_unavailable"
    asyncio.run(case())
