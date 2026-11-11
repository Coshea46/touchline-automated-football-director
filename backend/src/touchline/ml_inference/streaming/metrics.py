"""Bounded process/GPU samples; no dependency on an optional monitoring SDK."""

import subprocess
import time
from threading import Event, Lock, Thread


class ResourceSampler:
    def __init__(self, interval=1, *, query=None):
        self.interval, self.query = interval, query or self._query
        self._stop, self._lock = Event(), Lock()
        self._thread = None
        self._count = self._total = self._maximum = self._memory = self._errors = 0

    @staticmethod
    def _query():
        result = subprocess.run([
            'nvidia-smi', '--query-gpu=utilization.gpu,memory.used',
            '--format=csv,noheader,nounits', '-i', '0'],
            capture_output=True, text=True, timeout=.75, check=True)
        utilization, memory = [float(item.strip()) for item in result.stdout.strip().split(',')]
        if not 0 <= utilization <= 100 or not 0 <= memory <= 262144:
            raise ValueError('Invalid GPU resource sample')
        return utilization, memory

    def sample(self):
        try:
            utilization, memory = self.query()
        except Exception:  # noqa: BLE001 -- monitoring failure never fabricates measurements.
            with self._lock:
                self._errors += 1
            return
        with self._lock:
            self._count += 1
            self._total += utilization
            self._maximum = max(self._maximum, utilization)
            self._memory = max(self._memory, memory)

    def _run(self):
        while not self._stop.is_set():
            before = time.monotonic()
            self.sample()
            self._stop.wait(max(0, self.interval - (time.monotonic() - before)))

    def __enter__(self):
        self._thread = Thread(target=self._run, daemon=True, name='replay-gpu-samples')
        self._thread.start()
        return self

    @property
    def stats(self):
        with self._lock:
            return {'gpu_samples': self._count, 'gpu_query_errors': self._errors,
                    'gpu_utilization_mean_percent': self._total / self._count if self._count else None,
                    'gpu_utilization_max_percent': self._maximum if self._count else None,
                    'gpu_memory_max_mib': self._memory if self._count else None}

    def __exit__(self, *args):
        self._stop.set()
        self._thread.join(timeout=1)
