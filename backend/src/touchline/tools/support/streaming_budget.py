"""Historical streaming validation ledger; retains its separate $10 policy."""

import json
import math
import time
from pathlib import Path

from touchline.ml_inference.streaming.contract import MAX_WORKER_RATE
from touchline.paths import INFERENCE_ARTIFACTS_DIR
from touchline.tools.support.storage import atomic_json

ARTIFACTS = INFERENCE_ARTIFACTS_DIR / "overhaul"


def save(name, value, *, directory=None):
    directory = Path(directory) if directory is not None else ARTIFACTS
    atomic_json(directory / name, value, unique=False, trailing_newline=False)


class StreamingBudget:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else ARTIFACTS
        path = self.directory / 'budget.json'
        self.data = json.loads(path.read_text()) if path.exists() else {
            'ceiling_usd': 10, 'entries': [], 'rates': {'T4': .000164, 'cpu': .0000131, 'GiB': .00000222},
            'note': 'Task-side conservative estimate, not provider billing/hard cap',
            'pricing_source': 'https://modal.com/pricing'}
        if self.data['ceiling_usd'] != 10:
            raise RuntimeError('Unexpected task budget ceiling')
        prior = INFERENCE_ARTIFACTS_DIR / 't4-av-validation/budget.json'
        self.data['historical_inference_estimate_usd'] = json.loads(prior.read_text()).get('combined_inference_estimate_usd', 0) if prior.exists() else 0

    def reserve(self, name, estimate):
        if not math.isfinite(estimate) or estimate <= 0:
            raise ValueError('Reservation must be finite and positive')
        if sum(row['estimated_usd'] for row in self.data['entries']) + estimate > 10:
            raise RuntimeError('$10 inference task ceiling reached; no further cloud work')
        row = {'name': name, 'estimated_usd': estimate, 'state': 'reserved', 'at': time.time()}
        self.data['entries'].append(row)
        self.flush()
        return row

    def settle(self, row, elapsed, workers=1):
        row.update(state='returned', elapsed_seconds=elapsed,
                   estimated_usd=min(row['estimated_usd'], (elapsed + 30) * MAX_WORKER_RATE * workers))
        self.flush()

    def flush(self):
        self.data['estimated_usd'] = sum(row['estimated_usd'] for row in self.data['entries'])
        save('budget.json', self.data, directory=self.directory)

