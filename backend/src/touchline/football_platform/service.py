"""Bounded, asynchronous cache for real standings and licensed news metadata."""

import asyncio
import copy
import functools
import os
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from touchline.football_platform.catalog import COMPETITIONS, DEFAULT_COMPETITION_CODES, search_catalog, team_aliases
from touchline.football_platform.providers import FOOTBALL_ATTRIBUTION, GUARDIAN_ATTRIBUTION, NEWS_TOPICS, JSONTransport, ProviderError, news_payload, standings_payload


@dataclass(frozen=True)
class _Cached:
    data: dict
    fetched_at: float


class PlatformService:
    """No credentials are projected into API responses or exception messages.

    Quotas apply to this process: football 10/minute, Guardian 1/second and
    500/UTC day. Other applications sharing a provider key also consume its
    allowance, so provider 429s are respected independently. There are no retry
    loops, periodic polls, disk content caches, or background catalog crawls.
    """

    MAX_CACHE = 64
    MAX_INFLIGHT = 8
    HARD_RETENTION_SECONDS = 24 * 3600

    def __init__(self, *, football_key=None, guardian_key=None, transport=None, clock=time.time):
        self._football_key = football_key or None
        self._guardian_key = guardian_key or None
        self._transport = transport or JSONTransport()
        self._clock = clock
        self._cache = OrderedDict()
        self._failures = OrderedDict()
        self._expiry_handles = {}
        self._inflight = {}
        self._provider_retry_until = {}
        self._quota = {"football-data.org": deque(), "guardian": deque()}
        self._guardian_day = None
        self._guardian_calls = 0
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="platform-provider")
        self._closed = False

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        return cls(football_key=env.get("FOOTBALL_DATA_API_KEY"), guardian_key=env.get("GUARDIAN_API_KEY"))

    async def close(self):
        self._closed = True
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._executor.shutdown(wait=False, cancel_futures=True)
        for handle in self._expiry_handles.values():
            handle.cancel()
        self._expiry_handles.clear()
        self._cache.clear()
        self._failures.clear()

    def _purge(self):
        now = self._clock()
        for key, entry in list(self._cache.items()):
            if now - entry.fetched_at >= self.HARD_RETENTION_SECONDS:
                del self._cache[key]
                handle = self._expiry_handles.pop(key, None)
                if handle:
                    handle.cancel()

    def _expire(self, key, fetched_at):
        entry = self._cache.get(key)
        if entry and entry.fetched_at == fetched_at:
            self._cache.pop(key, None)
            self._expiry_handles.pop(key, None)

    def _remember_failure(self, key, provider, reason, retry):
        self._failures[key] = (reason, self._clock() + retry)
        self._failures.move_to_end(key)
        while len(self._failures) > self.MAX_CACHE:
            self._failures.popitem(last=False)
        # A provider embargo applies across search terms, so changing a query
        # cannot circumvent a 429 or repeatedly send a rejected credential.
        if reason in ("rate_limited", "access_denied"):
            self._provider_retry_until[provider] = (reason, self._clock() + retry)

    def _base(self, provider):
        guardian = provider == "guardian"
        return {"provider": provider, "attribution": copy.deepcopy(
            GUARDIAN_ATTRIBUTION if guardian else FOOTBALL_ATTRIBUTION),
                "setup_url": "https://open-platform.theguardian.com/access/" if guardian
                else "https://www.football-data.org/client/register",
                "fetched_at": None, "stale": False}

    def _unavailable(self, provider, reason, retry=30):
        messages = {
            "missing_key": "Set GUARDIAN_API_KEY on the backend to load news." if provider == "guardian"
            else "Set FOOTBALL_DATA_API_KEY on the backend to load current standings.",
            "access_denied": "The provider rejected access. Check the backend key and its competition/content permissions.",
            "rate_limited": "The provider request allowance is temporarily exhausted. Please try again later.",
            "unavailable": "The provider is temporarily unavailable. No replacement data has been generated.",
        }
        return {**self._base(provider), "status": reason if reason in ("missing_key", "rate_limited")
                else "unavailable", "error_code": reason, "message": messages.get(reason, messages["unavailable"]),
                "retry_after_seconds": retry, **({"articles": []} if provider == "guardian" else {"tables": []})}

    def _reserve(self, provider):
        now = self._clock()
        window = 1 if provider == "guardian" else 60
        limit = 1 if provider == "guardian" else 10
        calls = self._quota[provider]
        while calls and now - calls[0] >= window:
            calls.popleft()
        if len(calls) >= limit:
            raise ProviderError("rate_limited", max(1, int(window - (now - calls[0])) + 1))
        if provider == "guardian":
            day = int(now // 86400)
            if self._guardian_day != day:
                self._guardian_day, self._guardian_calls = day, 0
            if self._guardian_calls >= 500:
                raise ProviderError("rate_limited", min(3600, int((day + 1) * 86400 - now) + 1))
            self._guardian_calls += 1
        calls.append(now)

    async def _fetch(self, key, provider, path, params, headers, parser, ttl):
        self._purge()
        now = self._clock()
        cached = self._cache.get(key)
        if cached and now - cached.fetched_at < ttl:
            self._cache.move_to_end(key)
            return copy.deepcopy(cached.data)
        if self._closed:
            return self._unavailable(provider, "unavailable")
        credentials = self._guardian_key if provider == "guardian" else self._football_key
        if not credentials:
            return self._unavailable(provider, "missing_key", 0)
        if key in self._inflight:
            return copy.deepcopy(await asyncio.shield(self._inflight[key]))
        embargo = self._provider_retry_until.get(provider)
        if embargo and embargo[1] > now:
            return self._fallback(provider, cached, embargo[0], max(1, int(embargo[1] - now)))
        failed = self._failures.get(key)
        if failed and failed[1] > now:
            return self._fallback(provider, cached, failed[0], max(1, int(failed[1] - now)))
        if len(self._inflight) >= self.MAX_INFLIGHT:
            return self._fallback(provider, cached, "rate_limited", 1)

        async def load():
            try:
                self._reserve(provider)
                raw = await asyncio.get_running_loop().run_in_executor(
                    self._executor, functools.partial(self._transport.get, provider, path, params, headers))
                data = {**self._base(provider), **parser(raw), "status": "ok", "fetched_at": self._clock()}
                self._cache[key] = _Cached(data, data["fetched_at"])
                self._cache.move_to_end(key)
                handle = self._expiry_handles.pop(key, None)
                if handle:
                    handle.cancel()
                self._expiry_handles[key] = asyncio.get_running_loop().call_later(
                    self.HARD_RETENTION_SECONDS, self._expire, key, data["fetched_at"])
                while len(self._cache) > self.MAX_CACHE:
                    expired_key, _ = self._cache.popitem(last=False)
                    self._expiry_handles.pop(expired_key).cancel()
                self._failures.pop(key, None)
                return data
            except ProviderError as exc:
                retry = exc.retry_after
                self._remember_failure(key, provider, exc.reason, retry)
                return self._fallback(provider, cached, exc.reason, retry)
            except Exception:  # noqa: BLE001 - credential-bearing provider exceptions are never exposed
                # Provider/library exceptions can embed authenticated URLs. Only
                # our fixed, user-actionable error codes cross the API boundary.
                self._remember_failure(key, provider, "unavailable", 30)
                return self._fallback(provider, cached, "unavailable", 30)

        task = asyncio.create_task(load(), name="platform-provider-load")
        self._inflight[key] = task
        try:
            return copy.deepcopy(await asyncio.shield(task))
        finally:
            if task.done():
                self._inflight.pop(key, None)
            else:
                task.add_done_callback(lambda completed: self._inflight.pop(key, None))

    def _fallback(self, provider, cached, reason, retry):
        if cached and self._clock() - cached.fetched_at < self.HARD_RETENTION_SECONDS:
            return {**copy.deepcopy(cached.data), "stale": True, "provider_status": reason,
                    "retry_after_seconds": retry,
                    "message": "Showing previously fetched provider data; refresh is temporarily unavailable."}
        return self._unavailable(provider, reason, retry)

    async def standings(self, competition="PL"):
        if not isinstance(competition, str) or competition not in COMPETITIONS:
            raise ValueError("invalid_competition")
        result = await self._fetch(("standings", competition), "football-data.org",
                                   f"competitions/{competition}/standings", {},
                                   {"X-Auth-Token": self._football_key or ""},
                                   lambda raw: standings_payload(raw, competition), 900)
        result.setdefault("competition", copy.deepcopy(COMPETITIONS[competition]))
        result.setdefault("season", None)
        return result

    async def news(self, topic="football", query=""):
        if not isinstance(topic, str) or topic not in NEWS_TOPICS:
            raise ValueError("invalid_topic")
        if not isinstance(query, str) or len(query) > 120 or any(ord(char) < 32 for char in query):
            raise ValueError("invalid_query")
        query = " ".join(query.split())
        params = {"api-key": self._guardian_key or "", "section": "football", "order-by": "newest",
                  "page-size": "12", "show-fields": "byline,copyright", **NEWS_TOPICS[topic]}
        if query:
            params["q"] = query
        result = await self._fetch(("news", topic, query.casefold()), "guardian", "search", params, {},
                                   news_payload, 600)
        result.update(topic=topic, query=query, content_expires_at=(result["fetched_at"] + 86400)
                      if result["fetched_at"] is not None else None)
        return result

    async def catalog(self, query="", *, load_teams=False):
        if not isinstance(query, str) or len(query) > 120 or any(ord(char) < 32 for char in query):
            raise ValueError("invalid_query")
        load_results = []
        if load_teams and self._football_key:
            load_results = await asyncio.gather(*(self.standings(code) for code in DEFAULT_COMPETITION_CODES))
        self._purge()
        competitions, teams = copy.deepcopy(list(COMPETITIONS.values())), []
        timestamps, loaded_codes = [], set()
        for code in COMPETITIONS:
            cached = self._cache.get(("standings", code))
            if not cached:
                continue
            loaded_codes.add(code)
            timestamps.append(cached.fetched_at)
            competition = next(item for item in competitions if item["code"] == code)
            competition.update(cached.data["competition"])
            seen = set()
            for table in cached.data["tables"]:
                for row in table["rows"]:
                    team = row["team"]
                    if team["id"] in seen:
                        continue
                    seen.add(team["id"])
                    teams.append({"id": team["id"], "name": team["name"], "aliases": team_aliases(team),
                                  "competition_code": code, "fetched_at": cached.fetched_at})
        matches, ambiguous = search_catalog(query, competitions, teams)
        team_status = ("missing_key" if not self._football_key else
                       "ready" if set(DEFAULT_COMPETITION_CODES) <= loaded_codes else "partial" if loaded_codes else "unavailable")
        return {"status": "ok", "provider": "football-data.org", "competitions": competitions,
                "teams": teams, "matches": matches, "ambiguous": ambiguous,
                "team_catalog_status": team_status, "team_search_scope": "provider_standings",
                "loaded_competitions": sorted(loaded_codes), "fetched_at": min(timestamps) if timestamps else None,
                "stale": any(self._clock() - stamp >= 900 for stamp in timestamps),
                "message": "Club membership is supplied by current provider standings, independent of historical replays.",
                "provider_statuses": sorted({result["status"] for result in load_results}),
                "attribution": copy.deepcopy(FOOTBALL_ATTRIBUTION)}
