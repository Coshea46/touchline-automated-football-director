"""Bounded public admission with durable, non-refundable AI reservations.

One hosted container owns this SQLite ledger. Its deployment must mount durable
state and commit it before returning paid authorization. A failed persistence
callback disables further paid starts until the process is replaced.
"""

import asyncio
from collections import OrderedDict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import uuid


class AdmissionDenied(RuntimeError):
    def __init__(self, code, retry_after=0):
        self.code = code
        self.retry_after = max(0, int(retry_after))
        super().__init__(code)


class AdmissionPolicy:
    AI_SECONDS = 1200
    # Independent, non-refundable component starts. Four worst-case L4 workers
    # cost $2.1087072 base for 1200+330 seconds, plus 15% EU placement. Scribe
    # 1200 seconds costs $.13; standing warm-pool billing is separate.
    # Historical .60/.70 combined reservations retain their exact stored values.
    AI_RESERVATION_USD = 2.60
    CAPTION_RESERVATION_USD = .14
    MAX_STARTS_PER_HOUR = 10
    # Retain uncertain leases beyond the GPU's duration + startup/cleanup bounds.
    LEASE_SECONDS = 1680

    def __init__(self, db_path=None, *, commit=None, env=None, now=time.time,
                 monotonic=time.monotonic):
        env = os.environ if env is None else env
        if env.get("TOUCHLINE_AI_BUDGET_MODE", "lifetime") != "lifetime":
            raise ValueError("daily_usage_requires_dynamodb")
        self.daily_budget = self._amount(env.get("TOUCHLINE_AI_DAILY_BUDGET_USD", "1.20"))
        self.total_budget = self._amount(env.get("TOUCHLINE_AI_TOTAL_BUDGET_USD", "3.00"))
        self._path = Path(db_path or env.get("TOUCHLINE_ADMISSION_DB", "/app/state/admission.sqlite3"))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._commit, self.now, self.monotonic = commit, now, monotonic
        self._lock = asyncio.Lock()
        # A durable Volume commit must never delay another visitor's heartbeat.
        self._rate_lock = asyncio.Lock()
        self._db_lock = threading.RLock()
        self._buckets = OrderedDict()
        self._persistence_failed = False
        self._initialize()

    @staticmethod
    def _amount(value):
        result = float(value)
        if not math.isfinite(result) or not 0 <= result <= 100:
            raise ValueError("invalid_public_ai_budget")
        return result

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self._path, timeout=3)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._db_lock, self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS creations (peer TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS creations_time ON creations(created);
                CREATE TABLE IF NOT EXISTS permits (
                    session TEXT PRIMARY KEY, peer TEXT NOT NULL, created REAL NOT NULL,
                    day TEXT NOT NULL, amount REAL NOT NULL, expires REAL NOT NULL,
                    closed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS permit_components (
                    permit_id TEXT PRIMARY KEY, visitor_id TEXT NOT NULL,
                    component TEXT NOT NULL CHECK(component IN ('director','captions')),
                    FOREIGN KEY(permit_id) REFERENCES permits(session));
                CREATE INDEX IF NOT EXISTS permit_components_visitor
                    ON permit_components(visitor_id, component);
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES ('peer_salt', ?)", (secrets.token_hex(32),))
            self._salt = db.execute("SELECT value FROM settings WHERE name='peer_salt'").fetchone()[0]

    def peer_key(self, peer):
        # Raw client addresses never enter the ledger or error messages.
        return hashlib.sha256((self._salt + str(peer or "unknown")[:256]).encode()).hexdigest()

    async def _persist(self):
        if self._commit is not None:
            try:
                await self._native(self._commit)
            except asyncio.CancelledError:
                self._persistence_failed = True
                raise
            except Exception:
                self._persistence_failed = True
                raise AdmissionDenied("admission_storage_unavailable") from None

    @staticmethod
    async def _native(call):
        # Cancellation cannot interrupt SQLite or a Volume commit. Keep the
        # caller's lock until native work has stopped touching the ledger.
        task = asyncio.create_task(asyncio.to_thread(call))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done() and task.cancelled():
                    raise
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError() from None
                raise
        if cancelled:
            raise asyncio.CancelledError()
        return result

    def _rate(self, key, limit, window):
        now = self.monotonic()
        queue = self._buckets.get(key)
        if queue is None:
            # Refuse fresh keys when saturated; evicting hot keys would bypass limits.
            if len(self._buckets) >= 2048:
                stale = [name for name, values in self._buckets.items()
                         if not values or values[-1] <= now - 600]
                for name in stale:
                    self._buckets.pop(name, None)
                if len(self._buckets) >= 2048:
                    raise AdmissionDenied("rate_limit_capacity", 60)
            queue = self._buckets[key] = deque()
        while queue and queue[0] <= now - window:
            queue.popleft()
        if len(queue) >= limit:
            raise AdmissionDenied("rate_limited", math.ceil(queue[0] + window - now))
        queue.append(now)

    async def allow_connection(self, peer):
        async with self._rate_lock:
            self._rate("connect:" + self.peer_key(peer), 30, 60)
            self._rate("connect:global", 100, 60)

    async def allow_message(self, session_id):
        async with self._rate_lock:
            self._rate("message:" + str(session_id)[:100], 40, 1)

    async def allow_http(self, peer):
        async with self._rate_lock:
            self._rate("http:" + self.peer_key(peer), 60, 60)
            self._rate("http:global", 300, 60)

    async def admit_new(self, peer):
        async with self._lock:
            key, now = self.peer_key(peer), self.now()
            def write():
                with self._db_lock, self._connect() as db:
                    db.execute("DELETE FROM creations WHERE created <= ?", (now - 600,))
                    rows = db.execute("SELECT peer, created FROM creations").fetchall()
                    own = [created for who, created in rows if who == key]
                    if len(own) >= 3:
                        raise AdmissionDenied("session_rate_limited", math.ceil(min(own) + 600 - now))
                    if len(rows) >= 20:
                        raise AdmissionDenied("session_rate_limited", math.ceil(min(row[1] for row in rows) + 600 - now))
                    db.execute("INSERT INTO creations VALUES (?, ?)", (key, now))
            await self._native(write)
            await self._persist()
            return key

    async def acquire_ai(self, visitor_id, peer, *, component="director"):
        if component not in ("director", "captions"):
            raise ValueError("invalid_ai_component")
        if not isinstance(visitor_id, str) or not 1 <= len(visitor_id) <= 120:
            raise ValueError("invalid_ai_visitor")
        amount = self.AI_RESERVATION_USD if component == "director" else self.CAPTION_RESERVATION_USD
        async with self._lock:
            if self._persistence_failed:
                raise AdmissionDenied("admission_storage_unavailable")
            now = self.now()
            day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
            permit_id = uuid.uuid4().hex
            def write():
                with self._db_lock, self._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    # Old combined permits have no mapping and their session
                    # key is the visitor ID. Preserve that unresolved ownership
                    # for both components; never block unrelated visitors.
                    active = db.execute("""SELECT MAX(p.expires) FROM permits p
                        LEFT JOIN permit_components c ON c.permit_id=p.session
                        WHERE p.closed=0 AND ((c.visitor_id=? AND c.component=?)
                            OR (c.permit_id IS NULL AND p.session=?))""",
                        (visitor_id, component, visitor_id)).fetchone()[0]
                    if active is not None:
                        if active <= now:
                            raise AdmissionDenied("ai_cleanup_pending")
                        raise AdmissionDenied("ai_busy", math.ceil(active - now))
                    starts, first = db.execute("SELECT COUNT(*),MIN(created) FROM permits WHERE peer=? AND created>?",
                                               (peer, now - 3600)).fetchone()
                    if starts >= self.MAX_STARTS_PER_HOUR:
                        raise AdmissionDenied("ai_rate_limited", math.ceil(first + 3600 - now))
                    total = db.execute("SELECT COALESCE(SUM(amount),0) FROM permits").fetchone()[0]
                    daily = db.execute("SELECT COALESCE(SUM(amount),0) FROM permits WHERE day=?", (day,)).fetchone()[0]
                    if total + amount > self.total_budget + 1e-9:
                        raise AdmissionDenied("ai_budget_exhausted")
                    if daily + amount > self.daily_budget + 1e-9:
                        raise AdmissionDenied("ai_daily_limit", 86400)
                    db.execute("INSERT INTO permits VALUES (?,?,?,?,?,?,0)",
                               (permit_id, peer, now, day, amount, now + self.LEASE_SECONDS))
                    db.execute("INSERT INTO permit_components VALUES (?,?,?)", (permit_id, visitor_id, component))
            await self._native(write)
            # An interrupted or failed commit never grants paid authorization.
            await self._persist()
            return {"permit_id": permit_id, "component": component,
                    "duration_seconds": self.AI_SECONDS, "reserved_usd": amount,
                    "expires_at": now + self.AI_SECONDS}

    async def release_ai(self, permit_id, confirmed=False):
        if not confirmed:
            return
        async with self._lock:
            def write():
                with self._db_lock, self._connect() as db:
                    db.execute("UPDATE permits SET closed=1 WHERE session=?", (permit_id,))
            await self._native(write)
            await self._persist()

    async def close(self):
        # Connections are operation-scoped; there is no persistent SDK handle.
        return None
