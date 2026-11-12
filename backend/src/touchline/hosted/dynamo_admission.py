"""Atomic public admission without a filesystem commit on the request path.

Schema version 1, one DynamoDB table with string ``PK`` and ``SK``:

* config/settings: schema_version=1, migration_ready=True, peer_salt.
* budget/total and budget/day#YYYY-MM-DD: amount_microusd, version.
* peer#HASH/starts: events (Unix seconds), version.
* creations/global and creations/peer#HASH: events (Unix seconds), version.
* permit#ID/record: visitor_id, component, peer, created, day,
  amount_microusd, expires, closed, version, owner_keys=[{PK, SK}].
* visitor#ID/owner#COMPONENT: permit_id, expires, closed, version.

Permanent permit records retain every historical amount and timestamp. Migrated
unmapped combined permits use component='legacy' and both owner keys. Expired
unclosed ownership remains blocked; budget counters never decrease. The ready
marker is written last by the separate, stopped-app migration procedure.
"""

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal
import math
import os
import re
import threading
import time
import uuid

from touchline.hosted.admission import AdmissionDenied, AdmissionPolicy


SCHEMA_VERSION = 1
TABLE_ENV = "TOUCHLINE_ADMISSION_DYNAMODB_TABLE"
MICRO_USD = 1_000_000
MAX_COLLISION_ATTEMPTS = 5


def _serialize(values):
    from boto3.dynamodb.types import TypeSerializer

    serializer = TypeSerializer()
    return {key: serializer.serialize(value) for key, value in values.items()}


def _deserialize(values):
    from boto3.dynamodb.types import TypeDeserializer

    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(value) for key, value in values.items()}


def _number(value):
    return Decimal(str(value))


def _micro(value):
    amount = Decimal(str(value)) * MICRO_USD
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise ValueError("invalid_public_ai_budget_precision")
    return int(amount)


class DynamoAdmissionPolicy(AdmissionPolicy):
    """Reuse only the local rate helpers; never create/open a SQLite ledger."""

    def __init__(self, table_name=None, *, client=None, client_factory=None,
                 env=None, now=time.time, monotonic=time.monotonic):
        self.env = os.environ if env is None else env
        self.table_name = table_name or self.env.get(TABLE_ENV)
        if not isinstance(self.table_name, str) or re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", self.table_name) is None:
            raise ValueError("invalid_admission_table")
        if client is not None and client_factory is not None:
            raise ValueError("choose_one_admission_client")
        self.daily_budget = self._amount(self.env.get("TOUCHLINE_AI_DAILY_BUDGET_USD", "1.20"))
        self.total_budget = self._amount(self.env.get("TOUCHLINE_AI_TOTAL_BUDGET_USD", "3.00"))
        self._daily_micro, self._total_micro = _micro(self.daily_budget), _micro(self.total_budget)
        self.now, self.monotonic = now, monotonic
        self._rate_lock, self._warm_lock = asyncio.Lock(), asyncio.Lock()
        self._buckets = OrderedDict()
        self._client, self._client_factory = client, client_factory
        self._client_lock = threading.RLock()
        self._provider = None
        self._salt = None
        self._closed = False
        self._closing = False
        self._native_condition = threading.Condition()
        self._native_count = 0

    async def _native(self, call):
        def run():
            with self._native_condition:
                if self._closing:
                    raise AdmissionDenied("admission_storage_unavailable")
                self._native_count += 1
            try:
                return call()
            finally:
                with self._native_condition:
                    self._native_count -= 1
                    self._native_condition.notify_all()
        return await AdmissionPolicy._native(run)

    def _get_client(self):
        with self._client_lock:
            if self._closed:
                raise AdmissionDenied("admission_storage_unavailable")
            if self._client is None:
                if self._client_factory is not None:
                    self._client = self._client_factory()
                else:
                    from botocore.config import Config
                    from touchline.storage import S3Access

                    # Reuse the existing refreshable Modal OIDC credential
                    # provider without making any S3 request. Hosted factories
                    # may instead inject their app-owned shared AWS session.
                    self._provider = S3Access(self.env)
                    self._client = self._provider._get_session().client(
                        "dynamodb", region_name=self._provider.region,
                        config=Config(connect_timeout=3, read_timeout=5,
                                      retries={"max_attempts": 0}))
            return self._client

    def _get(self, pk, sk):
        response = self._get_client().get_item(TableName=self.table_name,
            Key=_serialize({"PK": pk, "SK": sk}), ConsistentRead=True)
        item = response.get("Item")
        return _deserialize(item) if item else None

    async def warm(self):
        if self._salt is not None:
            return
        async with self._warm_lock:
            if self._salt is not None:
                return
            try:
                settings = await self._native(lambda: self._get("config", "settings"))
                total = await self._native(lambda: self._get("budget", "total"))
                if (not settings or settings.get("schema_version") != SCHEMA_VERSION
                        or settings.get("migration_ready") is not True
                        or not isinstance(settings.get("peer_salt"), str)
                        or not 1 <= len(settings["peer_salt"]) <= 256 or total is None):
                    raise ValueError("admission_migration_not_ready")
                self._counter(total)
                self._salt = settings["peer_salt"]
            except asyncio.CancelledError:
                raise
            except Exception:
                raise AdmissionDenied("admission_storage_unavailable") from None

    async def allow_connection(self, peer):
        await self.warm()
        await super().allow_connection(peer)

    async def allow_http(self, peer):
        await self.warm()
        await super().allow_http(peer)

    @staticmethod
    def _version(item):
        if item is None:
            return None
        value = item.get("version")
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or value < 0 or int(value) != value:
            raise ValueError("invalid_admission_version")
        return int(value)

    @classmethod
    def _counter(cls, item):
        if item is None:
            return 0
        cls._version(item)
        value = item.get("amount_microusd")
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or value < 0 or int(value) != value:
            raise ValueError("invalid_admission_counter")
        return int(value)

    @classmethod
    def _events(cls, item, after):
        if item is None:
            return []
        cls._version(item)
        events = item.get("events")
        if not isinstance(events, list) or len(events) > 1000:
            raise ValueError("invalid_admission_events")
        if any(isinstance(value, bool) or not isinstance(value, (int, Decimal))
               or not math.isfinite(value) for value in events):
            raise ValueError("invalid_admission_events")
        return sorted(value for value in events if value > after)

    def _put(self, pk, sk, previous, fields, *, remaining=None, unique=False):
        version = self._version(previous)
        item = {"PK": pk, "SK": sk, **fields, "version": 0 if version is None else version + 1}
        put = {"TableName": self.table_name, "Item": _serialize(item)}
        if unique or previous is None:
            put["ConditionExpression"] = "attribute_not_exists(PK)"
        else:
            put.update(ConditionExpression="#version = :version",
                       ExpressionAttributeNames={"#version": "version"},
                       ExpressionAttributeValues=_serialize({":version": version}))
        if remaining is not None and previous is not None:
            put["ConditionExpression"] += " AND #amount <= :remaining"
            put["ExpressionAttributeNames"]["#amount"] = "amount_microusd"
            put["ExpressionAttributeValues"].update(_serialize({":remaining": remaining}))
        return {"Put": put}

    @staticmethod
    def _collision(exc):
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return False
        error = response.get("Error")
        code = error.get("Code") if isinstance(error, dict) else None
        if code == "TransactionConflictException":
            return True
        reasons = response.get("CancellationReasons")
        return (code == "TransactionCanceledException" and isinstance(reasons, list)
                and all(isinstance(reason, dict) for reason in reasons)
                and any(reason.get("Code") in ("ConditionalCheckFailed", "TransactionConflict") for reason in reasons)
                and all(reason.get("Code") in ("None", "ConditionalCheckFailed", "TransactionConflict") for reason in reasons))

    def _write(self, writes, token):
        self._get_client().transact_write_items(TransactItems=writes, ClientRequestToken=token)

    async def _retry(self, prepare):
        await self.warm()
        operation_id = uuid.uuid4().hex[:30]
        for attempt in range(MAX_COLLISION_ATTEMPTS):
            try:
                # Reads and the transaction stay together in one off-loop
                # operation. CAS protects concurrent containers/policy objects.
                return await self._native(lambda: prepare(f"{operation_id}-{attempt:02}"))
            except AdmissionDenied:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._collision(exc):
                    continue
                raise AdmissionDenied("admission_storage_unavailable") from None
        raise AdmissionDenied("admission_storage_unavailable")

    async def admit_new(self, peer):
        await self.warm()
        key = self.peer_key(peer)
        def prepare(token):
            now = _number(self.now())
            global_item = self._get("creations", "global")
            peer_item = self._get("creations", "peer#" + key)
            global_events = self._events(global_item, now - 600)
            own = self._events(peer_item, now - 600)
            for events, limit in ((own, 3), (global_events, 20)):
                if len(events) >= limit:
                    raise AdmissionDenied("session_rate_limited", math.ceil(events[0] + 600 - now))
            self._write([
                self._put("creations", "global", global_item, {"events": global_events + [now]}),
                self._put("creations", "peer#" + key, peer_item, {"events": own + [now]})], token)
            return key
        return await self._retry(prepare)

    async def acquire_ai(self, visitor_id, peer, *, component="director"):
        if component not in ("director", "captions"):
            raise ValueError("invalid_ai_component")
        if not isinstance(visitor_id, str) or not 1 <= len(visitor_id) <= 120:
            raise ValueError("invalid_ai_visitor")
        if not isinstance(peer, str) or not 1 <= len(peer) <= 256:
            raise ValueError("invalid_ai_peer")
        amount = self.AI_RESERVATION_USD if component == "director" else self.CAPTION_RESERVATION_USD
        amount_micro, permit_id = _micro(amount), uuid.uuid4().hex
        def prepare(token):
            now = _number(self.now())
            day = datetime.fromtimestamp(float(now), timezone.utc).date().isoformat()
            owner_pk, owner_sk = "visitor#" + visitor_id, "owner#" + component
            owner = self._get(owner_pk, owner_sk)
            if owner is not None:
                self._version(owner)
                if owner.get("closed") is not True:
                    expires = owner["expires"]
                    if expires <= now:
                        raise AdmissionDenied("ai_cleanup_pending")
                    raise AdmissionDenied("ai_busy", math.ceil(expires - now))
            peer_pk = "peer#" + peer
            starts = self._get(peer_pk, "starts")
            events = self._events(starts, now - 3600)
            if len(events) >= self.MAX_STARTS_PER_HOUR:
                raise AdmissionDenied("ai_rate_limited", math.ceil(events[0] + 3600 - now))
            total, daily = self._get("budget", "total"), self._get("budget", "day#" + day)
            if total is None:
                raise ValueError("missing_admission_total")
            total_amount, daily_amount = self._counter(total), self._counter(daily)
            if total_amount + amount_micro > self._total_micro:
                raise AdmissionDenied("ai_budget_exhausted")
            if daily_amount + amount_micro > self._daily_micro:
                raise AdmissionDenied("ai_daily_limit", 86400)
            owner_keys = [{"PK": owner_pk, "SK": owner_sk}]
            self._write([
                self._put("budget", "total", total, {"amount_microusd": total_amount + amount_micro},
                          remaining=self._total_micro - amount_micro),
                self._put("budget", "day#" + day, daily, {"amount_microusd": daily_amount + amount_micro},
                          remaining=self._daily_micro - amount_micro),
                self._put(peer_pk, "starts", starts, {"events": events + [now]}),
                self._put(owner_pk, owner_sk, owner, {"permit_id": permit_id,
                          "expires": now + self.LEASE_SECONDS, "closed": False}),
                self._put("permit#" + permit_id, "record", None, {"visitor_id": visitor_id,
                    "component": component, "peer": peer, "created": now, "day": day,
                    "amount_microusd": amount_micro, "expires": now + self.LEASE_SECONDS,
                    "closed": False, "owner_keys": owner_keys}, unique=True)], token)
            return {"permit_id": permit_id, "component": component,
                    "duration_seconds": self.AI_SECONDS, "reserved_usd": amount,
                    "expires_at": float(now + self.AI_SECONDS)}
        return await self._retry(prepare)

    async def release_ai(self, permit_id, confirmed=False):
        if not confirmed:
            return
        if not isinstance(permit_id, str) or not 1 <= len(permit_id) <= 120:
            raise ValueError("invalid_ai_permit")
        def prepare(token):
            pk = "permit#" + permit_id
            permit = self._get(pk, "record")
            if permit is None or permit.get("closed") is True:
                return
            owner_keys = permit.get("owner_keys")
            if not isinstance(owner_keys, list) or not 1 <= len(owner_keys) <= 2:
                raise ValueError("invalid_admission_ownership")
            fields = {key: value for key, value in permit.items() if key not in ("PK", "SK", "version")}
            writes = [self._put(pk, "record", permit, {**fields, "closed": True})]
            for key in owner_keys:
                if (not isinstance(key, dict) or set(key) != {"PK", "SK"}
                        or key["PK"] != "visitor#" + permit["visitor_id"]
                        or key["SK"] not in ("owner#director", "owner#captions")):
                    raise ValueError("invalid_admission_ownership")
                owner = self._get(key["PK"], key["SK"])
                if owner and owner.get("permit_id") == permit_id:
                    writes.append(self._put(key["PK"], key["SK"], owner,
                        {"permit_id": permit_id, "expires": owner["expires"], "closed": True}))
            self._write(writes, token)
        await self._retry(prepare)

    async def close(self):
        def finish():
            with self._native_condition:
                self._closing = True
                while self._native_count:
                    self._native_condition.wait()
            with self._client_lock:
                if self._closed:
                    return
                self._closed = True
                try:
                    if self._client is not None:
                        self._client.close()
                finally:
                    self._client = None
                    if self._provider is not None:
                        self._provider.close()
                        self._provider = None
        try:
            await AdmissionPolicy._native(finish)
        except Exception:
            raise AdmissionDenied("admission_storage_unavailable") from None
