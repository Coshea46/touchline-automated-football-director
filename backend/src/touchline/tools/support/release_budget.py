"""Selected-model release ledger; retains its separate locked $3 policy."""

import json
import math
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from touchline.ml_inference.streaming.contract import MAX_GPU_WORKERS, MAX_WORKER_RATE
from touchline.paths import INFERENCE_ARTIFACTS_DIR
from touchline.tools.support.storage import atomic_json as _save

ARTIFACTS = INFERENCE_ARTIFACTS_DIR / "winning-model"
CEILING_USD = 3.0


class ReleaseBudget:
    """Persistent task estimate with atomic writes and fail-closed concurrent updates.

    A crash can leave the lock: inspect it manually rather than starting overlapping
    paid work. Unconfirmed cleanup retains the complete reservation. Estimates are
    conservative planning records, not provider billing or a provider-side hard cap.
    """

    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else ARTIFACTS
        self.path = self.directory / "budget.json"
        self.data = self._read()

    def _read(self):
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
            "task": "winning-model", "ceiling_usd": CEILING_USD, "entries": [],
            "note": "Separate task estimate; not provider billing or a provider-side hard cap",
        }
        if data.get("task") != "winning-model" or data.get("ceiling_usd") != CEILING_USD:
            raise RuntimeError("Unexpected winning-model task ledger")
        total = 0.0
        for row in data["entries"]:
            amount = row.get("estimated_usd")
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
                raise RuntimeError("Invalid winning-model budget entry")
            total += amount
        if total > CEILING_USD + 1e-9:
            raise RuntimeError("Winning-model $3 ceiling already exceeded")
        data["estimated_usd"] = total
        return data

    @contextmanager
    def _transaction(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        lock = self.directory / "budget.lock"
        try:
            stream = lock.open("x", encoding="utf-8")
        except FileExistsError:
            raise RuntimeError("Winning-model ledger is locked; inspect before new cloud work") from None
        try:
            with stream:
                stream.write(str(os.getpid()))
            self.data = self._read()
            yield
            self.data["estimated_usd"] = sum(row["estimated_usd"] for row in self.data["entries"])
            _save(self.path, self.data)
        finally:
            lock.unlink()

    def reserve(self, action, amount):
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
            raise ValueError("Reservation must be finite and positive")
        with self._transaction():
            if self.data["estimated_usd"] + amount > CEILING_USD + 1e-9:
                raise RuntimeError("Winning-model $3 task ceiling reached; no cloud call started")
            row = {"id": uuid.uuid4().hex, "action": action, "reserved_usd": amount,
                   "estimated_usd": amount, "state": "reserved", "at": time.time()}
            self.data["entries"].append(row)
        return row

    def update(self, row, **fields):
        if "estimated_usd" in fields or "reserved_usd" in fields:
            raise ValueError("Only confirmed settlement may change a reservation")
        with self._transaction():
            entry = next(item for item in self.data["entries"] if item["id"] == row["id"])
            entry.update(fields)

    def settle(self, row, elapsed_seconds, *, workers=1, confirmed=False):
        if (isinstance(elapsed_seconds, bool) or not isinstance(elapsed_seconds, (int, float))
                or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0
                or type(workers) is not int or not 1 <= workers <= MAX_GPU_WORKERS):
            raise ValueError("Invalid bounded settlement duration or worker count")
        with self._transaction():
            entry = next(item for item in self.data["entries"] if item["id"] == row["id"])
            entry.update(elapsed_seconds=elapsed_seconds, cleanup_confirmed=confirmed is True)
            if confirmed is True:
                entry.update(state="completed", estimated_usd=min(
                    entry["reserved_usd"], (elapsed_seconds + 30) * MAX_WORKER_RATE * workers))
            else:
                entry.update(state="cleanup-unconfirmed", estimated_usd=entry["reserved_usd"])

