"""Capability-bound visitor ownership, finite lifetimes and paid cleanup."""

import asyncio
import hashlib
import hmac
import inspect
import secrets
import time
import uuid
from dataclasses import dataclass, field


class SessionDenied(Exception):
    def __init__(self, code, retry_after=None):
        self.code, self.retry_after = code, retry_after
        super().__init__(code)


def public_code(code):
    return {"session_rate_limited": "rate_limited", "rate_limit_capacity": "rate_limited",
        "ai_rate_limited": "rate_limited", "ai_session_used": "ai_finished",
        "ai_budget_exhausted": "budget_exhausted", "ai_daily_limit": "budget_exhausted",
        "ai_cleanup_pending": "ai_busy", "admission_storage_unavailable": "server_unavailable"}.get(code, code)


@dataclass
class Visitor:
    session: object
    token_hash: bytes = field(repr=False)
    peer: str = field(repr=False)
    expires_at: float
    disconnected_at: float | None = None
    connection: object = None
    connection_id: str | None = None
    ai_active: bool = False
    ai_consumed: bool = False
    ai_deadline: float | None = None
    ai_reason: str = "awaiting_start"
    ai_permit: str | None = field(default=None, repr=False)
    ai_cleanup_confirmed: bool = True
    startup_pending: bool = True
    stop_task: object = None
    captions_active: bool = False
    captions_deadline: float | None = None
    captions_permit: str | None = field(default=None, repr=False)
    captions_cleanup_confirmed: bool = True
    captions_stop_task: object = None
    closing: bool = False
    caption_service: object = None
    caption_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def _digest(token):
    return hashlib.sha256(token.encode("ascii")).digest()


class SessionManager:
    def __init__(self, session_factory, admission, *, now=time.time,
                 ttl_seconds=1200, grace_seconds=60, max_sessions=10):
        self.factory, self.admission, self.now = session_factory, admission, now
        self.ttl, self.grace, self.max_sessions = ttl_seconds, grace_seconds, max_sessions
        self.visitors = {}
        self._pending = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self._cleanup = set()
        self._creations = set()

    def snapshot(self, visitor, payload):
        if payload.get("type") != "session":
            return payload
        remaining = max(0, visitor.ai_deadline - self.now()) if visitor.ai_active else 1200
        result = {**payload, "session_expires_at": visitor.expires_at,
            "ai_limit_seconds": 1200, "ai_remaining_seconds": remaining,
            "ai_active": visitor.ai_active, "ai_reason": visitor.ai_reason,
            "controls_enabled": visitor.connection is not None}
        if not visitor.ai_active and visitor.session._prepared_config is not None:
            result.update(auto_can_start=visitor.ai_cleanup_confirmed,
                          auto_start_reason="available" if visitor.ai_cleanup_confirmed else "stopping")
        return result

    def _publish(self, visitor, payload):
        if visitor.connection is not None and not visitor.closing:
            visitor.connection.queue(self.snapshot(visitor, payload))

    async def _guard(self, session_id):
        visitor = self.visitors.get(session_id)
        if (visitor is None or visitor.closing or visitor.connection is None
                or not visitor.ai_active or self.now() >= visitor.ai_deadline):
            return "ai_finished"
        return None

    async def attach(self, hello, peer, connection):
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            raise SessionDenied("invalid_hello")
        if set(hello) - {"type", "session_id", "resume_token"}:
            raise SessionDenied("invalid_hello")
        resumed = "session_id" in hello or "resume_token" in hello
        token = hello.get("resume_token")
        session_id = hello.get("session_id")
        if resumed:
            if (not isinstance(session_id, str) or len(session_id) != 32
                    or not isinstance(token, str) or not 40 <= len(token) <= 100 or not token.isascii()):
                raise SessionDenied("resume_rejected")
            async with self._lock:
                visitor = self.visitors.get(session_id)
                if visitor is None:
                    raise SessionDenied("session_expired")
                if not hmac.compare_digest(visitor.token_hash, _digest(token)):
                    raise SessionDenied("resume_rejected")
                if self._expired(visitor):
                    self._schedule_close(visitor)
                    raise SessionDenied("session_expired")
                self._bind(visitor, connection)
        else:
            async with self._lock:
                if self._closed:
                    raise SessionDenied("server_unavailable")
                if len(self.visitors) + self._pending >= self.max_sessions:
                    raise SessionDenied("capacity_reached", 60)
                self._pending += 1
                creation = asyncio.current_task()
                self._creations.add(creation)
            session = None
            try:
                admitted_peer = await self.admission.admit_new(peer)
                session_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
                created = self.factory(session_id, lambda: self._guard(session_id))
                session = await created if inspect.isawaitable(created) else created
                visitor = Visitor(session, _digest(token), admitted_peer, self.now() + self.ttl)
                session.publish = lambda payload: self._publish(visitor, payload)
                async with self._lock:
                    if self._closed:
                        raise SessionDenied("server_unavailable")
                    self.visitors[session_id] = visitor
                    self._bind(visitor, connection)
                # Startup is an On preference, not permission to process unseen
                # footage. The first factual ready playback report admits it.
                session.director.set_auto(True)
                session.start()
            except BaseException:
                if session is not None:
                    self.visitors.pop(session.session_id, None)
                    await session.close()
                raise
            finally:
                self._pending -= 1
                self._creations.discard(creation)
        return visitor, {"type": "session_auth", "session_id": session_id,
            "resume_token": token, "resumed": resumed, "expires_at": visitor.expires_at}

    def _bind(self, visitor, connection):
        old, old_id = visitor.connection, visitor.connection_id
        if old is not None:
            # A valid capability can replace its own socket, never another
            # visitor. Stale handler finalizers cannot revoke this new owner.
            visitor.session.disconnect(old_id)
            self._track(asyncio.create_task(old.close("session_replaced")))
            self.finish_ai(visitor, "viewer_disconnected")
        visitor.connection, visitor.connection_id = connection, uuid.uuid4().hex
        visitor.disconnected_at = None
        connection.client_id = visitor.connection_id
        visitor.session.connect(visitor.connection_id)

    def owns(self, visitor, connection):
        return visitor.connection is connection and not visitor.closing

    async def receive(self, visitor, connection, message):
        if not self.owns(visitor, connection):
            raise SessionDenied("session_replaced")
        if self._expired(visitor):
            self._schedule_close(visitor)
            raise SessionDenied("session_expired")
        await self.admission.allow_message(visitor.session.session_id)
        # Admission can wait for a durable ledger commit. A capability resume
        # may have fenced this old socket while that lock was held.
        if not self.owns(visitor, connection):
            raise SessionDenied("session_replaced")
        if self._expired(visitor):
            self._schedule_close(visitor)
            raise SessionDenied("session_expired")
        if not isinstance(message, dict) or message.get("session_id") != visitor.session.session_id:
            return visitor.session._error("session_mismatch")
        kind = message.get("type")
        if kind in {"auto", "captions"} and type(message.get("enabled")) is bool:
            component = "director" if kind == "auto" else "captions"
            if kind == "auto":
                visitor.startup_pending = False
            if message["enabled"]:
                return await self._start_feature(visitor, connection, component)
            error = visitor.session.receive(connection.client_id, message)
            if component == "director":
                self.finish_director(visitor, "viewer_disabled")
            else:
                self.finish_captions(visitor, "viewer_disabled")
            return error
        if kind == "playback":
            error = visitor.session.receive(connection.client_id, message)
            if (error is None and visitor.startup_pending and visitor.session.playing
                    and not visitor.session.stalled and not visitor.session._controller_report_pending):
                visitor.startup_pending = False
                return await self._start_feature(visitor, connection, "director")
            return error
        return visitor.session.receive(connection.client_id, message)

    def _feature_error(self, visitor, component, code, retry_after=None):
        session = visitor.session
        if component == "director":
            visitor.ai_reason = code
            session.director.set_auto(False)
        else:
            session._caption_user_enabled = session._caption_enabled = False
            session._caption_status, session._caption_reason = "disabled", code
        session._publish_session()
        error = {**session._error(code), "feature": component}
        if retry_after is not None:
            error["retry_after"] = max(0, int(retry_after))
        return error

    async def _start_feature(self, visitor, connection, component):
        session = visitor.session
        director = component == "director"
        active = visitor.ai_active if director else visitor.captions_active
        deadline = visitor.ai_deadline if director else visitor.captions_deadline
        if active and self.now() >= deadline:
            (self.finish_director if director else self.finish_captions)(visitor, "ai_finished")
            return self._feature_error(visitor, component, "ai_busy")
        if not active:
            confirmed = visitor.ai_cleanup_confirmed if director else visitor.captions_cleanup_confirmed
            if not confirmed:
                return self._feature_error(visitor, component, "ai_busy")
            if director and (session._prepared_config is None or session._ended):
                return self._feature_error(visitor, component, "auto_unavailable")
            if not director and session.caption_worker is None:
                return self._feature_error(visitor, component, "captions_unavailable")
            try:
                lease = await self.admission.acquire_ai(session.session_id, visitor.peer, component=component)
            except Exception as exc:
                if not self.owns(visitor, connection) or self._expired(visitor):
                    raise SessionDenied("session_expired" if self._expired(visitor) else "session_replaced") from None
                code = public_code(getattr(exc, "code", None))
                if code not in {"ai_busy", "budget_exhausted", "rate_limited", "ai_finished", "server_unavailable"}:
                    raise
                return self._feature_error(visitor, component, code, getattr(exc, "retry_after", None))
            deadline = min(self.now() + min(1200, lease["duration_seconds"]),
                           lease.get("expires_at", float("inf")))
            if director:
                visitor.ai_permit = lease["permit_id"]
                visitor.ai_active, visitor.ai_consumed = True, True
                visitor.ai_deadline, visitor.ai_reason = deadline, "active"
            else:
                visitor.captions_permit, visitor.captions_active = lease["permit_id"], True
                visitor.captions_deadline = deadline
            expired = self._expired(visitor)
            if not self.owns(visitor, connection) or expired:
                (self.finish_director if director else self.finish_captions)(visitor, "viewer_disconnected")
                if expired:
                    self._schedule_close(visitor)
                raise SessionDenied("session_expired" if expired else "session_replaced")
            if self.now() >= deadline:
                (self.finish_director if director else self.finish_captions)(visitor, "ai_finished")
                return self._feature_error(visitor, component, "ai_finished")
            # A run started late in a replay still receives its full allowance.
            visitor.expires_at = max(visitor.expires_at, deadline + self.grace)
            try:
                if director:
                    session.reset_director_run()
                else:
                    session._caption_deadline = deadline
                    session.caption_worker.start()
            except Exception:
                (self.finish_director if director else self.finish_captions)(visitor, "start_failed")
                return self._feature_error(visitor, component, "server_unavailable")
        message = {"type": "auto" if director else "captions", "session_id": session.session_id, "enabled": True}
        error = session.receive(connection.client_id, message)
        if error:
            (self.finish_director if director else self.finish_captions)(visitor, "start_failed")
            return {**error, "feature": component}
        return None

    def finish_director(self, visitor, reason):
        if not visitor.ai_active:
            return visitor.stop_task
        visitor.ai_active, visitor.ai_reason = False, "stopping"
        visitor.ai_cleanup_confirmed = False
        session = visitor.session
        session._start_authorized = session._activation_requested = False
        session._stop_requested = True
        session.director.set_auto(False)
        permit = visitor.ai_permit
        visitor.stop_task = self._track(asyncio.create_task(self._stop_director(visitor, permit, reason)))
        self._publish(visitor, session.snapshot())
        return visitor.stop_task

    async def _stop_director(self, visitor, permit, reason):
        confirmed = False
        try:
            confirmed = await visitor.session.stop_monitoring(reason, stop_captions=False, preserve_prepared=True)
        finally:
            await self.admission.release_ai(permit, confirmed=confirmed is True)
            visitor.ai_cleanup_confirmed = confirmed is True
            visitor.ai_reason = "awaiting_start" if confirmed else "ai_busy"
            visitor.session._publish_session()

    def finish_captions(self, visitor, reason):
        if not visitor.captions_active:
            return visitor.captions_stop_task
        visitor.captions_active, visitor.captions_cleanup_confirmed = False, False
        session = visitor.session
        session._caption_user_enabled = session._caption_enabled = False
        session._caption_revoke(reason, status="disabled" if reason == "viewer_disabled" else "stopped")
        visitor.captions_stop_task = self._track(asyncio.create_task(
            self._stop_captions(visitor, visitor.captions_permit, session.caption_worker)))
        self._publish(visitor, session.snapshot())
        return visitor.captions_stop_task

    async def _stop_captions(self, visitor, permit, worker):
        session = visitor.session
        confirmed = worker is None
        try:
            if worker is not None:
                confirmed = await asyncio.wait_for(asyncio.to_thread(worker.close), timeout=4) is True
        except Exception:  # Provider details are never returned or logged.
            confirmed = False
        session.metrics["caption_stop_confirmed"] = confirmed
        await self.admission.release_ai(permit, confirmed=confirmed)
        visitor.captions_cleanup_confirmed = confirmed
        if confirmed and not visitor.closing:
            factory = getattr(session, "_hosted_caption_factory", None)
            if factory is not None:
                try:
                    session.caption_worker = factory()
                except Exception:
                    session.caption_worker = None
                    session._caption_reason = "runtime_unavailable"
        session._publish_session()

    def finish_ai(self, visitor, reason):
        visitor.startup_pending = False
        visitor.session.director.set_auto(False)
        tasks = [self.finish_director(visitor, reason), self.finish_captions(visitor, reason)]
        tasks = [task for task in tasks if task is not None]
        if not tasks:
            return None
        async def finish():
            await asyncio.gather(*tasks)
        return self._track(asyncio.create_task(finish()))

    def detach(self, visitor, connection):
        if not self.owns(visitor, connection):
            return
        visitor.session.disconnect(connection.client_id)
        visitor.connection = visitor.connection_id = None
        visitor.disconnected_at = self.now()
        self.finish_ai(visitor, "viewer_disconnected")

    def _expired(self, visitor):
        return (visitor.closing or self.now() >= visitor.expires_at or
                (visitor.disconnected_at is not None and self.now() - visitor.disconnected_at >= self.grace))

    def _track(self, task):
        self._cleanup.add(task)
        def done(finished):
            self._cleanup.discard(finished)
            if not finished.cancelled():
                finished.exception()  # Retrieve errors without leaking provider strings.
        task.add_done_callback(done)
        return task

    def _schedule_close(self, visitor):
        if not visitor.closing:
            visitor.closing = True
            self.visitors.pop(visitor.session.session_id, None)
            self._track(asyncio.create_task(self._close_visitor(visitor)))

    async def _close_visitor(self, visitor):
        connection = visitor.connection
        if connection is not None:
            visitor.session.disconnect(connection.client_id)
            visitor.connection = None
        stop = self.finish_ai(visitor, "session_expired")
        try:
            if connection is not None:
                await connection.close("session_expired")
        finally:
            try:
                if stop is not None:
                    await asyncio.shield(stop)
            finally:
                await visitor.session.close()

    async def sweep(self):
        for visitor in tuple(self.visitors.values()):
            if visitor.ai_active and (self.now() >= visitor.ai_deadline or visitor.session._finished
                    or visitor.session.state in ("error", "ended")):
                self.finish_director(visitor, "ai_finished")
            if visitor.captions_active and (self.now() >= visitor.captions_deadline
                    or visitor.session._caption_status in ("error", "unavailable", "stopped")):
                self.finish_captions(visitor, "ai_finished")
            if self._expired(visitor):
                self._schedule_close(visitor)

    async def close(self):
        self._closed = True
        creations = tuple(task for task in self._creations if task is not asyncio.current_task())
        for task in creations:
            task.cancel()
        await asyncio.gather(*creations, return_exceptions=True)
        for visitor in tuple(self.visitors.values()):
            self._schedule_close(visitor)
        while self._cleanup:
            await asyncio.gather(*tuple(self._cleanup), return_exceptions=True)
