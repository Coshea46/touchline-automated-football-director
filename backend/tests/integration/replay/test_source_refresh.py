"""Slow source authorization never blocks viewer leases or races client cleanup."""

import asyncio
from dataclasses import replace
from threading import Event
import time

import pytest

from touchline.replay import ReplaySession, SessionConfig
from touchline.replay import monitoring
from tests.support.replay.session import Clock, FakeBridge, configuration, playback, registry, scores


async def until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "Background source work did not reach the expected boundary"
        await asyncio.sleep(.005)


class RefreshRegistry:
    def __init__(self, *, fail_initial=False):
        self.base = registry()
        self.sources = self.base.sources
        self.refresh_due = self.block = self.running = self.closed = False
        self.fail_initial = fail_initial
        self.failures = self.calls = self.close_calls = 0
        self.close_failure = False
        self.entered, self.release, self.close_attempted = Event(), Event(), Event()

    def resolve(self):
        self.calls += 1
        if self.calls == 1 and not self.fail_initial:
            return self.base.resolve()
        self.running = True
        self.entered.set()
        try:
            if self.block:
                self.release.wait(4)
            assert not self.closed
            if self.fail_initial or self.failures:
                self.failures = max(0, self.failures - 1)
                raise RuntimeError("PRIVATE_PROVIDER_TOKEN&Signature=do-not-log")
            updated = tuple(replace(source, source_url=source.source_url + "?renewed=PRIVATE",
                                    playback_url=source.playback_url + "?renewed=PRIVATE")
                            for source in self.base.resolve())
            self.refresh_due = False
            return updated
        finally:
            self.running = False

    def needs_refresh(self):
        return self.refresh_due

    def close(self):
        assert not self.running, "S3 clients must outlive the thread using them"
        self.close_calls += 1
        self.close_attempted.set()
        if self.close_failure:
            raise RuntimeError("PRIVATE_CLOSE_TOKEN&Signature=do-not-log")
        self.closed = True


def test_slow_refresh_preserves_control_heartbeats_and_publishes_one_complete_source_set():
    async def scenario():
        sources, bridge, published = RefreshRegistry(), FakeBridge(), []
        session = await ReplaySession(sources, config=configuration(control_seconds=.1, heartbeat_seconds=1),
                                      bridge=bridge, publish=published.append).initialize()
        original = session.sources
        session.connect("viewer")
        session._handle, session._worker_ready = "owned", True
        session._ready_at, session.state = session.monotonic(), "running"
        sources.block = sources.refresh_due = True

        async def report_playback():
            while not session._closing:
                session.receive("viewer", playback(session))
                await asyncio.sleep(.02)

        reporting = asyncio.create_task(report_playback())
        session.start()
        try:
            await until(sources.entered.is_set)
            await until(lambda: len([call for call in bridge.calls if call[0] == "control"]) >= 12)
            controls = [call[1] for call in bridge.calls if call[0] == "control"]
            assert not sources.release.is_set() and sources.calls == 2
            assert session.sources == original
            assert all(value["active"] for value in controls)
            assert controls[-1]["expires_at"] > time.time()
            assert controls[-1]["updated_at"] - controls[0]["updated_at"] >= 1
            assert all(later["updated_at"] - earlier["updated_at"] < 1
                       for earlier, later in zip(controls, controls[1:]))
            sources.release.set()
            await until(lambda: session.sources != original)
            await until(lambda: any(call[0] == "control" and all("?renewed=" in item["source_url"]
                for item in call[1]["sources"]) for call in bridge.calls))
            assert session._source_refresh_failures == 0
            assert all("?renewed=" in source.source_url for source in session.sources)
            for payload in published:
                if payload.get("type") == "session":
                    flags = ["?renewed=" in item["url"] for item in payload["matches"]]
                    assert all(flags) or not any(flags)
        finally:
            sources.release.set()
            reporting.cancel()
            await asyncio.gather(reporting, return_exceptions=True)
            await session.close()
        assert sources.closed and sources.close_calls == 1

    asyncio.run(scenario())


def test_refresh_failures_keep_sources_and_use_bounded_backoff_without_provider_text(caplog):
    async def scenario():
        sources, clock = RefreshRegistry(), Clock()
        session = await ReplaySession(sources, config=SessionConfig(), now=clock, monotonic=clock).initialize()
        original = session.sources
        sources.refresh_due, sources.failures = True, 2
        try:
            await session._control_tick()
            await session._source_refresh_task
            assert session.sources == original and session._source_refresh_retry_at == 1005
            assert (session.state, session.error_code) == ("degraded", "source_refresh_failed")
            clock.value = 1004
            await session._control_tick()
            assert sources.calls == 2
            clock.value = 1005
            await session._control_tick()
            await session._source_refresh_task
            assert session._source_refresh_failures == 2 and session._source_refresh_retry_at == 1015
            clock.value = 1015
            await session._control_tick()
            await session._source_refresh_task
            assert session.sources != original
            assert session._source_refresh_failures == session._source_refresh_retry_at == 0
            assert (session.state, session.error_code) == ("disabled", None)
        finally:
            await session.close()

    asyncio.run(scenario())
    assert "RuntimeError" in caplog.text and "PRIVATE" not in caplog.text and "Signature" not in caplog.text


def test_ready_control_ticks_keep_source_errors_until_success_without_masking_other_failures():
    async def scenario():
        sources, clock, bridge = RefreshRegistry(), Clock(), FakeBridge()
        session = await ReplaySession(sources, config=configuration(), bridge=bridge,
                                      now=clock, monotonic=clock, session_id="session").initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session))
        session._handle, session._worker_ready, session._ready_at = "owned", True, clock.value - 2
        session.state = "running"
        sources.refresh_due, sources.failures = True, 1
        try:
            await session._control_tick()
            await session._source_refresh_task
            assert (session.state, session.error_code) == ("degraded", "source_refresh_failed")
            session._worker_ready = False
            ready = scores()
            ready.update(state="ready", scores=[])
            session.accept_snapshot(ready)
            assert session._worker_ready
            assert (session.state, session.error_code) == ("degraded", "source_refresh_failed")
            session._ready_at = clock.value - 2
            await session._control_tick()
            assert (session.state, session.error_code) == ("degraded", "source_refresh_failed")
            assert [call for call in bridge.calls if call[0] == "control"]
            clock.value = session._source_refresh_retry_at
            session.receive("viewer", playback(session))
            await session._control_tick()
            await session._source_refresh_task
            assert (session.state, session.error_code) == ("running", None)

            # A separately diagnosed worker error is authoritative even if a
            # media refresh also fails and later recovers.
            session.state, session.error_code = "error", "worker_error"
            sources.refresh_due, sources.failures = True, 1
            await session._control_tick()
            await session._source_refresh_task
            assert (session.state, session.error_code) == ("error", "worker_error")
            clock.value = session._source_refresh_retry_at
            session.receive("viewer", playback(session))
            await session._control_tick()
            await session._source_refresh_task
            assert (session.state, session.error_code) == ("error", "worker_error")
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("refresh_fails,close_fails", [(False, False), (True, False), (False, True)])
def test_close_defers_client_cleanup_until_blocked_refresh_finishes(monkeypatch, caplog, refresh_fails, close_fails):
    monkeypatch.setattr(monitoring, "SOURCE_CLEANUP_TIMEOUT_SECONDS", .02)

    async def scenario():
        sources, published, unhandled = RefreshRegistry(), [], []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _, context: unhandled.append(context))
        session = await ReplaySession(sources, publish=published.append).initialize()
        original = session.sources
        sources.block = sources.refresh_due = True
        sources.failures, sources.close_failure = int(refresh_fails), close_fails
        await session._control_tick()
        await until(sources.entered.is_set)
        try:
            await session.close()
            assert not sources.close_attempted.is_set() and sources.running
            assert session._source_refresh_task is None and session._closed
            assert session.metrics["source_cleanup_deferred"]
            published_at_close = len(published)
            sources.release.set()
            await until(sources.close_attempted.is_set)
            await until(lambda: session._source_cleanup_waiter.done())
            await asyncio.sleep(0)
            assert session.sources == original and len(published) == published_at_close
            assert session.metrics["source_cleanup_confirmed"] is (not close_fails)
            assert sources.close_calls == 1 and not unhandled
        finally:
            sources.release.set()

    asyncio.run(scenario())
    assert "PRIVATE" not in caplog.text and "Signature" not in caplog.text


def test_failed_initial_resolve_closes_its_owned_executor_and_clients():
    async def scenario():
        sources = RefreshRegistry(fail_initial=True)
        session = ReplaySession(sources)
        with pytest.raises(RuntimeError):
            await session.initialize()
        assert sources.closed and sources.close_calls == 1
        assert session._closed and session.director is None
        assert session.metrics["source_cleanup_confirmed"]
        await session.close()
        assert sources.close_calls == 1

    asyncio.run(scenario())


def test_cancelled_initial_resolve_closes_clients_only_after_its_native_thread_finishes(monkeypatch):
    monkeypatch.setattr(monitoring, "SOURCE_CLEANUP_TIMEOUT_SECONDS", .02)

    async def scenario():
        sources = RefreshRegistry(fail_initial=True)
        sources.block = True
        session = ReplaySession(sources)
        task = asyncio.create_task(session.initialize())
        await until(sources.entered.is_set)
        try:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert session._closed and not sources.close_attempted.is_set()
            sources.release.set()
            await until(lambda: sources.closed)
            await until(lambda: session.metrics["source_cleanup_confirmed"])
            assert sources.close_calls == 1
        finally:
            sources.release.set()

    asyncio.run(scenario())
