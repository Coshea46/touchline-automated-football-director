"""Offline tests for integration football_platform test_service."""


import asyncio
import json
import threading
import pytest
from touchline.football_platform import PlatformService
from touchline.football_platform.catalog import search_catalog, team_aliases
from touchline.football_platform.providers import ProviderError, standings_payload
from tests.support.football_platform import Clock, standing, Transport, run


def test_missing_keys_are_honest_and_never_attempt_http_even_for_full_catalog():
    async def check():
        transport = Transport()
        service = PlatformService(transport=transport)
        try:
            table, articles, catalog = await asyncio.gather(service.standings(), service.news(),
                                                           service.catalog(load_teams=True))
            assert table["status"] == articles["status"] == "missing_key"
            assert table["tables"] == articles["articles"] == []
            assert table["fetched_at"] is articles["fetched_at"] is None
            assert catalog["team_catalog_status"] == "missing_key" and catalog["teams"] == []
            assert len([item for item in catalog["competitions"] if item["default"]]) == 8
            assert transport.calls == []
        finally:
            await service.close()
    run(check())


def test_current_club_membership_only_from_provider_and_ambiguous_city_search():
    async def check():
        transport = Transport()
        service = PlatformService(football_key="test-only-football", transport=transport)
        try:
            assert (await service.catalog("Chelsea"))["teams"] == []
            await service.standings("PL")
            catalog = await service.catalog("Show me Chelsea's league")
            assert catalog["matches"][0]["team_name"] == "Chelsea FC"
            assert catalog["matches"][0]["competition_code"] == "PL"
            assert (await service.catalog("Show me Chelsea’s league"))["matches"][0]["team_name"] == "Chelsea FC"
            assert catalog["team_catalog_status"] == "partial"
            assert len(transport.calls) == 1
        finally:
            await service.close()
    run(check())
    teams = [{"id": index, "name": name, "competition_code": "PL",
              "aliases": team_aliases({"name": name})} for index, name in enumerate(
                  ("Manchester City FC", "Manchester United FC"), 1)]
    matches, ambiguous = search_catalog("Manchester", [], teams)
    assert ambiguous and {item["team_id"] for item in matches} == {1, 2}
    exact, ambiguous = search_catalog("man utd", [], teams)
    assert not ambiguous and exact[0]["team_id"] == 2
    assert "bayern munich" in team_aliases({"name": "FC Bayern München"})


def test_singleflight_cache_ownership_and_nonblocking_executor():
    async def check():
        transport, clock = Transport(), Clock()
        transport.block = threading.Event()
        service = PlatformService(football_key="test-only-football", transport=transport, clock=clock)
        try:
            first = asyncio.create_task(service.standings())
            second = asyncio.create_task(service.standings())
            for _ in range(50):
                if transport.calls:
                    break
                await asyncio.sleep(0.002)
            # The event loop remains responsive while real network work is blocked.
            assert not first.done() and not second.done()
            assert len(transport.calls) == 1
            transport.block.set()
            one, two = await asyncio.gather(first, second)
            one["tables"].clear()
            assert two["tables"]
            assert (await service.standings())["tables"]
            assert len(transport.calls) == 1
            assert "test-only-football" not in json.dumps(two)
        finally:
            transport.block.set()
            await service.close()
    run(check())


def test_stale_fallback_retry_suppression_and_guardian_hard_retention():
    async def check():
        transport, clock = Transport(), Clock()
        service = PlatformService(guardian_key="test-only-guardian", transport=transport, clock=clock)
        try:
            initial = await service.news()
            assert initial["content_expires_at"] == clock.now + 86400
            clock.now += 601
            transport.failure = ProviderError("rate_limited", 60)
            stale = await service.news()
            assert stale["status"] == "ok" and stale["stale"]
            assert stale["provider_status"] == "rate_limited" and stale["fetched_at"] == initial["fetched_at"]
            assert len(transport.calls) == 2
            assert (await service.news())["stale"]
            assert len(transport.calls) == 2
            clock.now = initial["content_expires_at"]
            expired = await service.news()
            assert expired["articles"] == [] and expired["fetched_at"] is None
            assert expired["status"] == "rate_limited" and not service._cache
        finally:
            await service.close()
    run(check())


def test_full_catalog_is_exactly_eight_calls_then_cached_and_keeps_real_teams():
    async def check():
        transport = Transport()
        service = PlatformService(football_key="test-only-football", transport=transport)
        try:
            loaded = await service.catalog(load_teams=True)
            assert loaded["team_catalog_status"] == "ready" and len(loaded["teams"]) == 16
            assert len(transport.calls) == 8
            again = await service.catalog(load_teams=True)
            assert len(again["loaded_competitions"]) == 8 and len(transport.calls) == 8
            await service.standings("PL")
            assert len(transport.calls) == 8
        finally:
            await service.close()
    run(check())


@pytest.mark.parametrize("operation_spec", [("standings", "https://attacker.example"),
                                     ("news", "unknown"), ("news_query", "x" * 121),
                                     ("catalog", "bad\nquery")])
def test_invalid_public_inputs_never_call_provider(operation_spec):
    async def check():
        transport = Transport()
        service = PlatformService(football_key="test", guardian_key="test", transport=transport)
        try:
            operation, value = operation_spec
            with pytest.raises(ValueError):
                if operation == "standings":
                    await service.standings(value)
                elif operation == "news":
                    await service.news(value)
                elif operation == "news_query":
                    await service.news(query=value)
                else:
                    await service.catalog(value)
            assert transport.calls == []
        finally:
            await service.close()
    run(check())


def test_provider_exception_secrets_are_sanitized_and_failures_negative_cached():
    async def check():
        transport = Transport()
        transport.failure = RuntimeError("https://content.guardianapis.com/search?api-key=DO-NOT-EXPOSE")
        service = PlatformService(guardian_key="DO-NOT-EXPOSE", transport=transport)
        try:
            result = await service.news()
            assert result["status"] == "unavailable" and "DO-NOT-EXPOSE" not in json.dumps(result)
            await service.news()
            assert len(transport.calls) == 1
        finally:
            await service.close()
    run(check())


def test_provider_retry_after_cannot_be_bypassed_by_changing_news_search():
    async def check():
        transport, clock = Transport(), Clock()
        transport.failure = ProviderError("rate_limited", 60)
        service = PlatformService(guardian_key="test-only-guardian", transport=transport, clock=clock)
        try:
            assert (await service.news(query="Chelsea"))["status"] == "rate_limited"
            clock.now += 2
            transport.failure = None
            assert (await service.news(query="Arsenal"))["status"] == "rate_limited"
            assert len(transport.calls) == 1
            clock.now += 60
            assert (await service.news(query="Arsenal"))["status"] == "ok"
            assert len(transport.calls) == 2
        finally:
            await service.close()
    run(check())


def test_guardian_content_is_deleted_when_idle_not_only_when_requested():
    async def check():
        service = PlatformService(guardian_key="test-only-guardian", transport=Transport())
        # Keep the deadline above Windows' event-loop clock resolution so the
        # scheduled expiry cannot run before the news caller resumes.
        service.HARD_RETENTION_SECONDS = 0.1
        try:
            assert (await service.news())["articles"]
            assert service._cache
            await asyncio.sleep(0.2)
            assert not service._cache and not service._expiry_handles
        finally:
            await service.close()
    run(check())


def test_explicit_champions_league_groups_are_preserved_not_flattened():
    async def check():
        service = PlatformService(football_key="test-only-football", transport=Transport())
        try:
            result = await service.standings("CL")
            assert result["competition"]["code"] == "CL"
            catalog = await service.catalog()
            optional = next(item for item in catalog["competitions"] if item["code"] == "CL")
            assert optional["default"] is False
        finally:
            await service.close()
    run(check())
    payload = standing("CL")
    payload["competition"]["type"] = "LEAGUE_CUP"
    payload["standings"][0].update(stage="LEAGUE_STAGE", group="League phase")
    result = standings_payload(payload, "CL")
    assert result["tables"][0]["stage"] == "LEAGUE_STAGE" and result["tables"][0]["group"] == "League phase"


def test_concurrent_complete_catalog_requests_coalesce_all_eight_provider_calls():
    async def check():
        transport = Transport()
        service = PlatformService(football_key="test-only-football", transport=transport)
        try:
            one, two = await asyncio.gather(service.catalog(load_teams=True), service.catalog(load_teams=True))
            assert one["team_catalog_status"] == two["team_catalog_status"] == "ready"
            assert len(transport.calls) == 8
        finally:
            await service.close()
    run(check())


def test_failure_cache_is_bounded_even_for_unexpected_provider_exceptions():
    async def check():
        transport, clock = Transport(), Clock()
        transport.failure = RuntimeError("test failure")
        service = PlatformService(guardian_key="test-only-guardian", transport=transport, clock=clock)
        try:
            for index in range(70):
                await service.news(query=f"query {index}")
                clock.now += 2
            assert len(service._failures) == service.MAX_CACHE and not service._inflight
        finally:
            await service.close()
    run(check())
