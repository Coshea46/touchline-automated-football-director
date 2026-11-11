"""Offline tests for unit football_platform test_quotas."""


import pytest
from touchline.football_platform import PlatformService
from touchline.football_platform.providers import ProviderError
from tests.support.football_platform import Clock, Transport, run


def test_guardian_per_second_and_daily_quota_no_retry_fanout():
    async def check():
        transport, clock = Transport(), Clock()
        service = PlatformService(guardian_key="test-only-guardian", transport=transport, clock=clock)
        try:
            assert (await service.news(query="Chelsea"))["status"] == "ok"
            assert (await service.news(query="Arsenal"))["status"] == "rate_limited"
            assert len(transport.calls) == 1
            clock.now += 2
            service._guardian_calls = 500
            assert (await service.news(query="Liverpool"))["status"] == "rate_limited"
            assert len(transport.calls) == 1
            clock.now = (int(clock.now // 86400) + 1) * 86400
            assert (await service.news(query="Liverpool"))["status"] == "ok"
            assert len(transport.calls) == 2
        finally:
            await service.close()
    run(check())


def test_football_quota_ten_per_rolling_minute_and_exact_boundary():
    service, clock = PlatformService(), Clock()
    service._clock = clock
    for _ in range(10):
        service._reserve("football-data.org")
    with pytest.raises(ProviderError, match="rate_limited"):
        service._reserve("football-data.org")
    clock.now += 59.99
    with pytest.raises(ProviderError):
        service._reserve("football-data.org")
    clock.now += 0.01
    service._reserve("football-data.org")
    run(service.close())
