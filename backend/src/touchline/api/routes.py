"""Application route composition; domain providers remain transport-independent."""

import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor

from touchline.football_platform.service import PlatformService


class ApplicationRoutes:
    def __init__(self, service=None, extra_routes=None):
        self.service = service or PlatformService.from_env()
        self.extra_routes = extra_routes or {}
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="touchline-api")

    async def close(self):
        try:
            await self.service.close()
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)

    async def dispatch(self, path, query):
        if path == "/api/catalog":
            if set(query) - {"q", "load_teams"} or query.get("load_teams", "0") not in ("0", "1"):
                raise ValueError("invalid_query")
            return await self.service.catalog(query.get("q", ""), load_teams=query.get("load_teams") == "1")
        if path == "/api/standings":
            if set(query) - {"competition"}:
                raise ValueError("invalid_query")
            return await self.service.standings(query.get("competition", "PL"))
        if path == "/api/news":
            if set(query) - {"topic", "q"}:
                raise ValueError("invalid_query")
            return await self.service.news(query.get("topic", "football"), query.get("q", ""))
        if path in self.extra_routes:
            callback = self.extra_routes[path]
            if inspect.iscoroutinefunction(callback):
                return await callback(query)
            # Caption handlers are cached local-file readers, not transcription
            # triggers. Keep even their initial filesystem read off the loop.
            return await asyncio.get_running_loop().run_in_executor(self._executor, callback, query)
        return None

