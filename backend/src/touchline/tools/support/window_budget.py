"""Historical complete-window validation ledger; retains its separate $5 policy."""

import json
import math
import time
from pathlib import Path

from touchline.paths import INFERENCE_ARTIFACTS_DIR
from touchline.tools.support.storage import atomic_json

ARTIFACTS = INFERENCE_ARTIFACTS_DIR / "t4-av-validation"
RATES = {"t4": 0.000164, "cpu": 0.0000131, "memory_gib": 0.00000222}
MAX_GPU_RATE = RATES["t4"] + 4 * RATES["cpu"] + 8 * RATES["memory_gib"]


def save(name, value, *, directory=None):
    directory = Path(directory) if directory is not None else ARTIFACTS
    atomic_json(directory / name, value, unique=False, trailing_newline=False)


class WindowValidationBudget:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else ARTIFACTS
        path = self.directory / "budget.json"
        self.data = json.loads(path.read_text()) if path.exists() else {
            "ceiling_usd": 5.0, "entries": [], "rates": RATES,
            "pricing_source": "https://modal.com/pricing",
            "note": "Controller estimates only; not provider billing or a provider-enforced cap",
        }
        if self.data["ceiling_usd"] != 5:
            raise RuntimeError("Unexpected validation budget ceiling")
        previous = INFERENCE_ARTIFACTS_DIR / "cloud-validation/budget.json"
        self.data["prior_task_estimate_usd"] = (json.loads(previous.read_text())["estimated_usd"]
                                                if previous.exists() else 0)

    def reserve(self, name, amount):
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("Cloud reservation must be finite and positive")
        total = sum(row["estimated_usd"] for row in self.data["entries"])
        if total + amount > 5:
            raise RuntimeError("$5 validation estimate exhausted; do not launch more cloud work")
        row = {"name": name, "estimated_usd": amount, "state": "reserved", "time": time.time()}
        self.data["entries"].append(row)
        self.flush()
        return row

    def settle(self, row, elapsed):
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("Cloud elapsed time must be finite and nonnegative")
        row.update(state="returned", elapsed_seconds=elapsed,
                   estimated_usd=min(row["estimated_usd"], (elapsed + 30) * MAX_GPU_RATE))
        self.flush()

    def flush(self):
        self.data["estimated_usd"] = sum(row["estimated_usd"] for row in self.data["entries"])
        self.data["combined_inference_estimate_usd"] = self.data["estimated_usd"] + self.data["prior_task_estimate_usd"]
        save("budget.json", self.data, directory=self.directory)


class BudgetedMethod:
    def __init__(self, method, budget, name):
        self.method, self.budget, self.name = method, budget, name

    def spawn(self, *args):
        row = self.budget.reserve(self.name, (180 + 300 + 30) * MAX_GPU_RATE)
        started = time.perf_counter()
        handle = self.method.spawn(*args)
        row["call_id"] = handle.object_id
        self.budget.flush()
        budget = self.budget

        class Handle:
            def get(self, timeout):
                try:
                    result = handle.get(timeout=timeout)
                except Exception as exc:
                    row.update(state="failed-or-unconfirmed", error_type=type(exc).__name__)
                    budget.flush()
                    raise
                budget.settle(row, time.perf_counter() - started)
                return result

            def cancel(self, **kwargs):
                return handle.cancel(**kwargs)

        return Handle()

