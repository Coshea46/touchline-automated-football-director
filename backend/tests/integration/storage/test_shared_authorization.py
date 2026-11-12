"""App credential reuse retains fresh per-visitor identity and ordered cleanup."""

import asyncio
from threading import Event, Lock

import pytest

from touchline.hosted.app import create_app
from touchline.hosted.runtime import create_source_access
from touchline.replay.sources import SourceRegistry
from touchline.storage import StorageAuthorizationError
from tests.support.hosted import Admission, Connection, Factory, Routes, until
from tests.support.storage import Clock, FakeSTS, oidc_access, response

pytestmark = pytest.mark.integration
ENV = {"MATCHPULSE_ALLOWED_ORIGINS": "https://portfolio.invalid",
    "S3_BUCKET_NAME": "synthetic-private-videos", "MATCHPULSE_SOURCE_ACCESS": "presigned",
    "AWS_DEFAULT_REGION": "eu-west-2", "MATCHPULSE_CAPTIONS_ENABLED": "false"}


async def lifespan_start(app):
    inputs, outputs = asyncio.Queue(), asyncio.Queue()
    async def send(event):
        await outputs.put(event)
    task = asyncio.create_task(app({"type": "lifespan"}, inputs.get, send))
    await inputs.put({"type": "lifespan.startup"})
    assert (await asyncio.wait_for(outputs.get(), 2))["type"] == "lifespan.startup.complete"
    return task, inputs, outputs


def test_startup_warms_one_credential_generation_and_two_visitors_still_issue_fourteen_fresh_heads():
    async def scenario():
        clock, sts = Clock(), FakeSTS([])
        sts.responses.append(response(clock))
        access, _, sessions, _, _ = oidc_access(clock, sts)
        app = create_app(env=ENV, admission=Admission(), routes=Routes(), source_access=access)
        task, inputs, outputs = await lifespan_start(app)
        try:
            assert len(sts.calls) == 1 and len(sessions[0].clients) == 2
            head, signer = sessions[0].clients
            assert head.calls == signer.calls == []  # Warmup never HEADs/signs an object.
            first, second = await asyncio.gather(
                app.sessions.attach({"type": "hello"}, "peer-a", Connection()),
                app.sessions.attach({"type": "hello"}, "peer-b", Connection()))
            first, second = first[0], second[0]
            assert first.session.registry._access is second.session.registry._access is access
            assert first.session.registry._identity_pins is not second.session.registry._identity_pins
            assert len(head.calls) == 14 and len(signer.calls) == 28
            assert len(sts.calls) == 1 and len(sessions[0].clients) == 2
            await first.session.close()
            assert not access.closed and not head.closed and not signer.closed
            access.presign_head(ENV["S3_BUCKET_NAME"], "match-1.mp4")
            assert not second.session._closed
        finally:
            await inputs.put({"type": "lifespan.shutdown"})
            assert (await asyncio.wait_for(outputs.get(), 3))["type"] == "lifespan.shutdown.complete"
            await task
        assert access.closed and app.source_access_close_confirmed is True
        assert all(client.closed and client.close_calls == 1 for client in sessions[0].clients)
    asyncio.run(scenario())


def test_warm_failure_is_sanitized_and_actual_visitor_authorization_can_retry(caplog):
    async def scenario():
        clock = Clock()
        sts = FakeSTS([RuntimeError("PRIVATE_TEST_OIDC&Signature=must-not-leak"), response(clock)])
        access, _, sessions, _, _ = oidc_access(clock, sts)
        app = create_app(env=ENV, admission=Admission(), routes=Routes(), source_access=access)
        task, inputs, outputs = await lifespan_start(app)
        try:
            assert app.source_warm_error == "aws_web_identity_exchange_failed"
            visitor, _ = await app.sessions.attach({"type": "hello"}, "peer", Connection())
            assert len(visitor.session.sources) == 7 and len(sts.calls) == 2
            assert len(sessions[0].clients[0].calls) == 7
            assert "PRIVATE_TEST" not in caplog.text and "Signature=" not in caplog.text
        finally:
            await inputs.put({"type": "lifespan.shutdown"})
            await outputs.get()
            await task
    asyncio.run(scenario())


def test_fresh_identity_and_credential_rotation_remain_per_visit_with_shared_access():
    clock = Clock()
    sts = FakeSTS([response(clock, lifetime=1200), lambda: response(clock, suffix="two")])
    access, env, sessions, _, _ = oidc_access(clock, sts)
    access.warm()
    first = SourceRegistry.from_env(ENV, s3_access=access, owns_access=False)
    second = None
    try:
        before = first.resolve()
        head, old_signer = sessions[0].clients
        original_head = head.head_object
        def changed_head(**kwargs):
            value = original_head(**kwargs)
            return {**value, "ETag": '"different-object"'}
        head.head_object = changed_head
        env["MODAL_IDENTITY_TOKEN"] = "PRIVATE_TEST_OIDC_ROTATED"
        clock.value += 600
        second = SourceRegistry.from_env(ENV, s3_access=access, owns_access=False)
        after = second.resolve()
        assert before[0].identity != after[0].identity
        assert len(head.calls) == 14 and len(sts.calls) == 2
        assert old_signer.closed and len(sessions[0].clients) == 3
        assert after[0].expires_at <= clock() + 3570
        with pytest.raises(StorageAuthorizationError, match="source_object_changed"):
            first.resolve()
    finally:
        first.close()
        if second is not None:
            second.close()
        access.close()


def test_app_shutdown_defers_sdk_close_until_cancelled_registry_native_heads_and_signing_finish(monkeypatch):
    from touchline.replay import monitoring
    monkeypatch.setattr(monitoring, "SOURCE_CLEANUP_TIMEOUT_SECONDS", .03)
    async def scenario():
        clock, sts = Clock(), FakeSTS([])
        sts.responses.append(response(clock))
        access, _, sessions, _, _ = oidc_access(clock, sts)
        app = create_app(env=ENV, admission=Admission(), routes=Routes(), source_access=access)
        await app._warm_source_access()
        head = sessions[0].clients[0]
        entered, release, closed = Event(), Event(), Event()
        lock, active = Lock(), [0]
        original_head, original_close = head.head_object, head.close
        def blocked_head(**kwargs):
            with lock:
                active[0] += 1
                entered.set()
            try:
                assert release.wait(3)
                assert not head.closed
                return original_head(**kwargs)
            finally:
                with lock:
                    active[0] -= 1
        def close():
            assert active[0] == 0
            original_close()
            closed.set()
        head.head_object, head.close = blocked_head, close
        creation = asyncio.create_task(app.sessions.attach({"type": "hello"}, "peer", Connection()))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await app.close()
            assert creation.cancelled() and app.source_access_close_confirmed is False
            assert not head.closed and not access.closed
        finally:
            release.set()
            await asyncio.gather(creation, return_exceptions=True)
            await until(closed.is_set)
        assert access.closed and all(client.closed for client in sessions[0].clients)
    asyncio.run(scenario())


def test_public_sources_and_custom_session_factories_stay_aws_lazy():
    assert create_source_access({**ENV, "MATCHPULSE_SOURCE_ACCESS": "public"}) is None
    async def scenario():
        app = create_app(env={"MATCHPULSE_ALLOWED_ORIGINS": ENV["MATCHPULSE_ALLOWED_ORIGINS"]},
                         admission=Admission(), session_factory=Factory(), routes=Routes())
        try:
            await app._warm_source_access()
            assert app.source_access is None
            await app.sessions.attach({"type": "hello"}, "peer", Connection())
        finally:
            await app.close()
    asyncio.run(scenario())
