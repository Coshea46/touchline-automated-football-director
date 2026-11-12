import asyncio
from pathlib import Path
import threading

import pytest

from touchline.hosted.admission import AdmissionDenied, AdmissionPolicy


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


def test_simultaneous_ai_requests_grant_only_one_and_persist_before_return(tmp_path):
    async def case():
        commits = []
        policy = AdmissionPolicy(tmp_path / "db", commit=lambda: commits.append("committed"))
        results = await asyncio.gather(policy.acquire_ai("a", "peer-a"),
                                       policy.acquire_ai("b", "peer-b"), return_exceptions=True)
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, AdmissionDenied) and result.code == "ai_busy" for result in results) == 1
        assert commits == ["committed"]
    asyncio.run(case())


def test_release_does_not_refund_or_allow_reuse_and_restart_cannot_reset_budget(tmp_path):
    async def case():
        path = tmp_path / "db"
        policy = AdmissionPolicy(path)
        await policy.acquire_ai("a", "p-a")
        await policy.release_ai("a", confirmed=True)
        with pytest.raises(AdmissionDenied, match="ai_session_used"):
            await policy.acquire_ai("a", "p-a")
        await policy.acquire_ai("b", "p-b")
        await policy.release_ai("b", confirmed=True)
        restored = AdmissionPolicy(path)
        with pytest.raises(AdmissionDenied, match="ai_daily_limit"):
            await restored.acquire_ai("c", "p-c")
        unrestricted_day = AdmissionPolicy(path, env={"TOUCHLINE_AI_DAILY_BUDGET_USD": "3",
                                                    "TOUCHLINE_AI_TOTAL_BUDGET_USD": "1.2"})
        with pytest.raises(AdmissionDenied, match="ai_budget_exhausted"):
            await unrestricted_day.acquire_ai("c", "p-c")
    asyncio.run(case())


def test_uncertain_cleanup_is_not_released_by_timeout_or_restart(tmp_path):
    async def case():
        path = tmp_path / "db"
        policy = AdmissionPolicy(path, now=lambda: 10000)
        await policy.acquire_ai("a", "p-a")
        await policy.release_ai("a", confirmed=False)
        restored = AdmissionPolicy(path, now=lambda: 20000)
        with pytest.raises(AdmissionDenied, match="ai_cleanup_pending"):
            await restored.acquire_ai("b", "p-b")
    asyncio.run(case())


def test_one_ip_cannot_restart_ai_using_new_session(tmp_path):
    async def case():
        policy = AdmissionPolicy(tmp_path / "db")
        await policy.acquire_ai("a", "same-peer")
        await policy.release_ai("a", confirmed=True)
        with pytest.raises(AdmissionDenied, match="ai_rate_limited"):
            await policy.acquire_ai("b", "same-peer")
    asyncio.run(case())


def test_failed_durable_commit_fails_closed_and_keeps_reservation(tmp_path):
    async def case():
        def fail():
            raise RuntimeError("PRIVATE_TOKEN_must_not_escape")
        path = tmp_path / "db"
        policy = AdmissionPolicy(path, commit=fail)
        with pytest.raises(AdmissionDenied) as caught:
            await policy.acquire_ai("a", "p-a")
        assert str(caught.value) == "admission_storage_unavailable"
        with pytest.raises(AdmissionDenied, match="admission_storage_unavailable"):
            await policy.acquire_ai("b", "p-b")
        with pytest.raises(AdmissionDenied, match="ai_busy"):
            await AdmissionPolicy(path).acquire_ai("b", "p-b")
    asyncio.run(case())


def test_frame_flood_and_connection_limits_are_bounded(tmp_path):
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
        policy = AdmissionPolicy(tmp_path / "db", commit=commit)
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
