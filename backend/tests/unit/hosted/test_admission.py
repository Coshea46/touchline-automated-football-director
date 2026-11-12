"""Durable independent component admission; no provider work is performed."""

import asyncio
from datetime import datetime, timezone
import sqlite3
import threading

import pytest

from touchline.hosted.admission import AdmissionDenied, AdmissionPolicy


FUNDED = {"TOUCHLINE_AI_DAILY_BUDGET_USD": "100", "TOUCHLINE_AI_TOTAL_BUDGET_USD": "100"}


def test_creation_quota_persists_and_does_not_store_raw_addresses(tmp_path):
    async def case():
        path = tmp_path / "admission.sqlite3"
        policy = AdmissionPolicy(path, now=lambda: 10000)
        for _ in range(3):
            key = await policy.admit_new("192.0.2.1")
        assert len(key) == 64
        restored = AdmissionPolicy(path, now=lambda: 10001)
        with pytest.raises(AdmissionDenied, match="session_rate_limited"):
            await restored.admit_new("192.0.2.1")
        assert b"192.0.2.1" not in path.read_bytes()
    asyncio.run(case())


def test_twenty_minute_component_reservations_cover_costs_and_funding_defaults_are_unchanged(tmp_path):
    from touchline.ml_inference.streaming.contract import MAX_WORKER_RATE
    from touchline.captions.limits import LIVE_PRICE_PER_HOUR_USD
    assert AdmissionPolicy.AI_SECONDS == 1200
    assert (1200 + 330) * 4 * MAX_WORKER_RATE == pytest.approx(2.1087072)
    assert (1200 + 330) * 4 * MAX_WORKER_RATE <= AdmissionPolicy.AI_RESERVATION_USD == 2.20
    assert 1200 * LIVE_PRICE_PER_HOUR_USD / 3600 <= AdmissionPolicy.CAPTION_RESERVATION_USD == .14
    async def case():
        policy = AdmissionPolicy(tmp_path / "db", env={})
        assert policy.daily_budget == 1.20 and policy.total_budget == 3
        with pytest.raises(AdmissionDenied, match="ai_daily_limit"):
            await policy.acquire_ai("director", "peer")
        lease = await policy.acquire_ai("captions", "peer", component="captions")
        assert lease["reserved_usd"] == .14 and lease["duration_seconds"] == 1200
    asyncio.run(case())


def test_parallel_visitors_both_start_and_each_commits_before_return(tmp_path):
    async def case():
        commits = []
        policy = AdmissionPolicy(tmp_path / "db", env=FUNDED, now=lambda: 10000,
                                 commit=lambda: commits.append("committed"))
        leases = await asyncio.gather(policy.acquire_ai("a", "peer-a"), policy.acquire_ai("b", "peer-b"))
        assert commits == ["committed", "committed"]
        assert len({lease["permit_id"] for lease in leases}) == 2
        assert all(lease["duration_seconds"] == 1200 and lease["reserved_usd"] == 2.20
                   and lease["expires_at"] == 11200 and lease["component"] == "director" for lease in leases)
    asyncio.run(case())


def test_parallel_budget_reservations_are_atomic_even_across_policy_instances(tmp_path):
    async def case():
        path = tmp_path / "db"
        env = {"TOUCHLINE_AI_DAILY_BUDGET_USD": "2.20", "TOUCHLINE_AI_TOTAL_BUDGET_USD": "2.20"}
        first, second = AdmissionPolicy(path, env=env), AdmissionPolicy(path, env=env)
        results = await asyncio.gather(first.acquire_ai("a", "peer-a"), second.acquire_ai("b", "peer-b"),
                                       return_exceptions=True)
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, AdmissionDenied) and result.code == "ai_budget_exhausted" for result in results) == 1
        with first._connect() as db:
            assert db.execute("SELECT COUNT(*),SUM(amount) FROM permits").fetchone() == (1, 2.20)
            assert db.execute("SELECT COUNT(*) FROM permit_components").fetchone()[0] == 1
    asyncio.run(case())


def test_open_component_blocks_only_its_owner_and_confirmed_release_can_restart_without_cooldown(tmp_path):
    async def case():
        policy = AdmissionPolicy(tmp_path / "db", env=FUNDED)
        director = await policy.acquire_ai("visitor", "same-peer")
        captions = await policy.acquire_ai("visitor", "same-peer", component="captions")
        other = await policy.acquire_ai("other", "other-peer")
        with pytest.raises(AdmissionDenied, match="ai_busy"):
            await policy.acquire_ai("visitor", "same-peer")
        await policy.release_ai(director["permit_id"], confirmed=True)
        restarted = await policy.acquire_ai("visitor", "same-peer")
        assert restarted["permit_id"] not in {lease["permit_id"] for lease in (director, captions, other)}
        with policy._connect() as db:
            rows = dict(db.execute("SELECT session,closed FROM permits"))
            assert rows[director["permit_id"]] == 1
            assert all(rows[lease["permit_id"]] == 0 for lease in (captions, other, restarted))
            assert db.execute("SELECT SUM(amount) FROM permits").fetchone()[0] == pytest.approx(6.74)
    asyncio.run(case())


def test_uncertain_cleanup_survives_restart_without_globally_blocking_other_components_or_visitors(tmp_path):
    async def case():
        path = tmp_path / "db"
        policy = AdmissionPolicy(path, env=FUNDED, now=lambda: 10000)
        first = await policy.acquire_ai("visitor", "peer")
        await policy.release_ai(first["permit_id"], confirmed=False)
        restored = AdmissionPolicy(path, env=FUNDED, now=lambda: 20000)
        with pytest.raises(AdmissionDenied, match="ai_cleanup_pending"):
            await restored.acquire_ai("visitor", "peer")
        await restored.acquire_ai("visitor", "peer", component="captions")
        await restored.acquire_ai("other", "other-peer")
        await restored.release_ai(first["permit_id"], confirmed=True)
        await restored.acquire_ai("visitor", "peer")
    asyncio.run(case())


def test_exactly_ten_director_or_caption_starts_share_one_ip_rolling_hour_without_cooldown(tmp_path):
    async def case():
        now = [10000.0]
        policy = AdmissionPolicy(tmp_path / "db", env=FUNDED, now=lambda: now[0])
        for index in range(10):
            lease = await policy.acquire_ai("visitor", "peer", component="director" if index % 2 else "captions")
            await policy.release_ai(lease["permit_id"], confirmed=True)
            now[0] += 1
        with pytest.raises(AdmissionDenied, match="ai_rate_limited") as caught:
            await policy.acquire_ai("new-visitor", "peer")
        assert caught.value.retry_after == 3590
        await policy.acquire_ai("different-visitor", "different-peer")
        restored = AdmissionPolicy(policy._path, env=FUNDED, now=lambda: 13600.0)
        await restored.acquire_ai("visitor", "peer")
        with pytest.raises(AdmissionDenied, match="ai_rate_limited"):
            await restored.acquire_ai("another", "peer", component="captions")
    asyncio.run(case())


def test_concurrent_tenth_start_is_atomic_across_policy_instances(tmp_path):
    async def case():
        path = tmp_path / "db"
        first = AdmissionPolicy(path, env=FUNDED, now=lambda: 10000)
        for _ in range(9):
            lease = await first.acquire_ai("historical-visitor", "same-peer", component="captions")
            await first.release_ai(lease["permit_id"], confirmed=True)
        second = AdmissionPolicy(path, env=FUNDED, now=lambda: 10000)
        results = await asyncio.gather(first.acquire_ai("visitor-a", "same-peer"),
            second.acquire_ai("visitor-b", "same-peer", component="captions"), return_exceptions=True)
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, AdmissionDenied) and result.code == "ai_rate_limited" for result in results) == 1
        with first._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM permits WHERE peer='same-peer'").fetchone()[0] == 10
    asyncio.run(case())


def test_historical_schema_rows_salt_and_creations_survive_and_count_toward_rate_and_budget(tmp_path):
    async def case():
        path, now = tmp_path / "db", [10000.0]
        day = datetime.fromtimestamp(now[0], timezone.utc).date().isoformat()
        old = [(f"old-{i}", "peer", now[0] - 100 * (i + 1), day,
                .60 if i % 2 else .70, now[0] - 10, 1) for i in range(9)]
        db = sqlite3.connect(path)
        try:
            with db:
                db.executescript("""
                    CREATE TABLE settings(name TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE creations(peer TEXT NOT NULL,created REAL NOT NULL);
                    CREATE TABLE permits(session TEXT PRIMARY KEY,peer TEXT NOT NULL,created REAL NOT NULL,
                        day TEXT NOT NULL,amount REAL NOT NULL,expires REAL NOT NULL,closed INTEGER NOT NULL DEFAULT 0);
                """)
                db.execute("INSERT INTO settings VALUES('peer_salt','synthetic-historical-salt')")
                db.execute("INSERT INTO creations VALUES('old-creation-peer',9999)")
                db.executemany("INSERT INTO permits VALUES(?,?,?,?,?,?,?)", old)
        finally:
            db.close()
        policy = AdmissionPolicy(path, env=FUNDED, now=lambda: now[0])
        assert policy._salt == "synthetic-historical-salt"
        lease = await policy.acquire_ai("new-visitor", "peer")
        with pytest.raises(AdmissionDenied, match="ai_rate_limited") as caught:
            await policy.acquire_ai("another", "peer", component="captions")
        assert caught.value.retry_after == 2700
        with policy._connect() as db:
            assert db.execute("SELECT * FROM permits WHERE session LIKE 'old-%' ORDER BY session").fetchall() == old
            assert db.execute("SELECT * FROM settings").fetchall() == [("peer_salt", "synthetic-historical-salt")]
            assert db.execute("SELECT * FROM creations").fetchall() == [("old-creation-peer", 9999)]
            assert db.execute("SELECT SUM(amount) FROM permits").fetchone()[0] == pytest.approx(sum(row[4] for row in old) + 2.20)
        restored = AdmissionPolicy(path, env={"TOUCHLINE_AI_DAILY_BUDGET_USD": "100",
            "TOUCHLINE_AI_TOTAL_BUDGET_USD": str(sum(row[4] for row in old) + 2.20)}, now=lambda: now[0])
        with pytest.raises(AdmissionDenied, match="ai_budget_exhausted"):
            await restored.acquire_ai("unrelated", "other-peer", component="captions")
        now[0] += 2700
        await policy.acquire_ai("new-visitor", "peer", component="captions")
        assert lease["permit_id"] != "new-visitor"
    asyncio.run(case())


def test_legacy_unclosed_combined_permit_blocks_its_visitor_only(tmp_path):
    async def case():
        policy = AdmissionPolicy(tmp_path / "db", env=FUNDED, now=lambda: 10000)
        with policy._connect() as db:
            db.execute("INSERT INTO permits VALUES(?,?,?,?,?,?,?)", ("old-visitor", "old-peer", 9000,
                "1970-01-01", .60, 9600, 0))
        for component in ("director", "captions"):
            with pytest.raises(AdmissionDenied, match="ai_cleanup_pending"):
                await policy.acquire_ai("old-visitor", "old-peer", component=component)
        await policy.acquire_ai("new-visitor", "other-peer")
    asyncio.run(case())


def test_default_lifetime_ceiling_never_refunds_closed_component_reservations(tmp_path):
    async def case():
        policy = AdmissionPolicy(tmp_path / "db", env={"TOUCHLINE_AI_DAILY_BUDGET_USD": "100"})
        director = await policy.acquire_ai("visitor", "peer")
        await policy.release_ai(director["permit_id"], confirmed=True)
        for _ in range(5):
            caption = await policy.acquire_ai("visitor", "peer", component="captions")
            await policy.release_ai(caption["permit_id"], confirmed=True)
        with pytest.raises(AdmissionDenied, match="ai_budget_exhausted"):
            await policy.acquire_ai("visitor", "peer", component="captions")
        with policy._connect() as db:
            assert db.execute("SELECT COUNT(*),SUM(amount),SUM(closed) FROM permits").fetchone() == (6, pytest.approx(2.90), 6)
    asyncio.run(case())


def test_failed_durable_commit_fails_closed_without_losing_component_ownership_or_reservation(tmp_path):
    async def case():
        def fail():
            raise RuntimeError("PRIVATE_TOKEN_must_not_escape")
        path = tmp_path / "db"
        policy = AdmissionPolicy(path, env=FUNDED, now=lambda: 10000, commit=fail)
        with pytest.raises(AdmissionDenied) as caught:
            await policy.acquire_ai("visitor", "peer")
        assert str(caught.value) == "admission_storage_unavailable"
        with pytest.raises(AdmissionDenied, match="admission_storage_unavailable"):
            await policy.acquire_ai("other", "other-peer")
        restored = AdmissionPolicy(path, env=FUNDED, now=lambda: 10000)
        with pytest.raises(AdmissionDenied, match="ai_busy"):
            await restored.acquire_ai("visitor", "peer")
        await restored.acquire_ai("other", "other-peer")
    asyncio.run(case())


def test_durable_commit_does_not_starve_existing_message_http_or_connection_admission(tmp_path):
    async def case():
        started, release = threading.Event(), threading.Event()
        def commit():
            started.set()
            assert release.wait(3)
        policy = AdmissionPolicy(tmp_path / "db", env=FUNDED, commit=commit)
        task = asyncio.create_task(policy.acquire_ai("new-visitor", "new-peer"))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            await asyncio.wait_for(asyncio.gather(policy.allow_message("existing"),
                policy.allow_connection("existing-peer"), policy.allow_http("existing-peer")), .2)
            assert not task.done()
        finally:
            release.set()
            await task
    asyncio.run(case())


def test_frame_flood_and_connection_limits_are_unchanged(tmp_path):
    async def case():
        policy = AdmissionPolicy(tmp_path / "db", monotonic=lambda: 1000)
        for _ in range(40):
            await policy.allow_message("a")
        with pytest.raises(AdmissionDenied, match="rate_limited"):
            await policy.allow_message("a")
        await policy.allow_message("b")
        for _ in range(30):
            await policy.allow_connection("peer")
        with pytest.raises(AdmissionDenied, match="rate_limited"):
            await policy.allow_connection("peer")
    asyncio.run(case())


def test_cancellation_waits_for_native_commit_and_never_grants(tmp_path):
    async def case():
        started, release = threading.Event(), threading.Event()
        def commit():
            started.set()
            release.wait(3)
        policy = AdmissionPolicy(tmp_path / "db", env=FUNDED, commit=commit)
        task = asyncio.create_task(policy.acquire_ai("a", "p-a"))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()
        task.cancel()
        contender = asyncio.create_task(policy.acquire_ai("b", "p-b"))
        await asyncio.sleep(.02)
        assert not task.done() and not contender.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(AdmissionDenied, match="admission_storage_unavailable"):
            await contender
    asyncio.run(case())


@pytest.mark.parametrize("component", [None, True, "combined", "unknown"])
def test_invalid_component_never_reserves_a_permit(component, tmp_path):
    async def case():
        policy = AdmissionPolicy(tmp_path / "db", env=FUNDED)
        with pytest.raises(ValueError, match="invalid_ai_component"):
            await policy.acquire_ai("visitor", "peer", component=component)
        with policy._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM permits").fetchone()[0] == 0
    asyncio.run(case())
