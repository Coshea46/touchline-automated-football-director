"""Offline transactional DynamoDB double with atomic condition evaluation."""

from copy import deepcopy
from decimal import Decimal
import threading

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError


def encode(item):
    return {key: TypeSerializer().serialize(value) for key, value in item.items()}


def decode(item):
    return {key: TypeDeserializer().deserialize(value) for key, value in item.items()}


def failure(code="TransactionCanceledException"):
    response = {"Error": {"Code": code, "Message": "PRIVATE_SIGNED_URL_TOKEN"}}
    if code == "TransactionCanceledException":
        response["CancellationReasons"] = [{"Code": "ConditionalCheckFailed"}]
    return ClientError(response, "TransactWriteItems")


class Dynamo:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()
        self.transactions, self.tokens = [], []
        self.reads = 0
        self.before_write = None
        self.collisions = 0
        self.fail_after_write = False
        self.fail_before_write = False
        self.closed = False
        self.seed("config", "settings", schema_version=1, migration_ready=True,
                  peer_salt="synthetic-preserved-peer-salt")
        self.seed("budget", "total", amount_microusd=0, version=0)

    def seed(self, pk, sk, **fields):
        self.items[(pk, sk)] = {"PK": pk, "SK": sk, **fields}

    def get_item(self, **request):
        assert request["ConsistentRead"] is True
        key = decode(request["Key"])
        with self.lock:
            self.reads += 1
            item = self.items.get((key["PK"], key["SK"]))
            return {"Item": encode(deepcopy(item))} if item else {}

    def transact_write_items(self, **request):
        if self.before_write:
            self.before_write()
        with self.lock:
            token = request["ClientRequestToken"]
            assert len(token) <= 36
            self.tokens.append(token)
            if self.fail_before_write:
                raise RuntimeError("PRIVATE_SIGNED_URL_TOKEN")
            if self.collisions:
                self.collisions -= 1
                raise failure()
            pending = []
            for operation in request["TransactItems"]:
                put = operation["Put"]
                item = decode(put["Item"])
                key = (item["PK"], item["SK"])
                assert key not in [name for name, _ in pending], "Dynamo rejects duplicate transaction keys"
                previous = self.items.get(key)
                if put["ConditionExpression"] == "attribute_not_exists(PK)":
                    if previous is not None:
                        raise failure()
                else:
                    values = decode(put["ExpressionAttributeValues"])
                    if previous is None or previous["version"] != values[":version"]:
                        raise failure()
                    if ":remaining" in values and previous["amount_microusd"] > values[":remaining"]:
                        raise failure()
                pending.append((key, item))
            self.items.update(deepcopy(pending))
            self.transactions.append(deepcopy(request))
            if self.fail_after_write:
                raise RuntimeError("PRIVATE_SIGNED_URL_TOKEN")

    def close(self):
        self.closed = True

    def permits(self):
        return [item for (pk, sk), item in self.items.items() if pk.startswith("permit#") and sk == "record"]

    def historical(self, permit_id, *, visitor="old-visitor", peer="old-peer",
                   component="legacy", amount=Decimal(".60"), created=Decimal("9900"),
                   expires=Decimal("9990"), closed=False):
        components = ("director", "captions") if component == "legacy" else (component,)
        keys = [{"PK": "visitor#" + visitor, "SK": "owner#" + value} for value in components]
        self.seed("permit#" + permit_id, "record", visitor_id=visitor, peer=peer,
            component=component, created=created, expires=expires, closed=closed,
            amount_microusd=int(amount * 1_000_000), day="1970-01-01", owner_keys=keys, version=0)
        for key in keys:
            self.seed(key["PK"], key["SK"], permit_id=permit_id, expires=expires, closed=closed, version=0)
        total = self.items[("budget", "total")]
        total["amount_microusd"] += int(amount * 1_000_000)
        daily = self.items.setdefault(("budget", "day#1970-01-01"), {
            "PK": "budget", "SK": "day#1970-01-01", "amount_microusd": 0, "version": 0})
        daily["amount_microusd"] += int(amount * 1_000_000)
        starts = self.items.setdefault(("peer#" + peer, "starts"), {
            "PK": "peer#" + peer, "SK": "starts", "events": [], "version": 0})
        starts["events"].append(created)
