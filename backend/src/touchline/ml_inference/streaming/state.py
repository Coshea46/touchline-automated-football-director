"""Bounded asynchronous IO for replay control and latest compact snapshots.

SDK calls run on daemon threads with separate copies of the invoking Modal
context. The processing loop never waits for a network call. The viewer's last
reported control and its original lease remain authoritative; no clock value
is extrapolated. Publication holds at most one pending and one in-flight item.
"""

import contextvars
import math
import time
from threading import Condition, Event, RLock, Thread, current_thread


def _primitive_copy(value, depth=0):
    """Copy JSON primitives while rejecting tensors, RGB/PCM arrays and objects."""
    if depth > 32:
        raise ValueError("Replay state exceeds the supported nesting depth")
    kind = type(value)
    if value is None or kind in (str, bool, int):
        return value
    if kind is float and math.isfinite(value):
        return value
    if kind is list:
        return [_primitive_copy(item, depth + 1) for item in value]
    if kind is dict and all(type(key) is str for key in value):
        return {key: _primitive_copy(item, depth + 1) for key, item in value.items()}
    raise TypeError("Replay state must contain only finite JSON primitives")


def _timeout(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{name} is outside the supported range")
    return float(value)


class LatestStateChannel:
    """Coalesce compact state around slow synchronous control/storage SDK calls.

    Use as a context manager, then ``wait_initial`` before starting the worker.
    ``control`` and ``publish`` perform no network IO. ``close`` shares one
    bounded join deadline across both threads. With ``flushlatest=True`` the
    newest pending snapshot is still attempted after an in-flight call returns;
    a stalled SDK call never holds the processing thread or shutdown open.
    """

    def __init__(self, control_reader, publisher, poll_seconds=0.1):
        if not callable(control_reader) or not callable(publisher):
            raise TypeError("Control reader and publisher must be callable")
        self.poll_seconds = _timeout(poll_seconds, "poll_seconds", positive=True)
        self._reader, self._publisher = control_reader, publisher
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._stop, self._initial = Event(), Event()
        self._control = self._pending = None
        self._inflight = False
        self._read_started = self._publication_started = None
        self._started = self._closing = False
        self._flushlatest = True
        self._control_thread = self._publication_thread = None
        self._metrics = {
            "control_reads": 0, "control_seconds": 0.0, "control_errors": 0,
            "publications": 0, "publication_seconds": 0.0, "publication_errors": 0,
            "max_pending": 0, "max_inflight": 0, "coalesced_snapshots": 0,
            "last_control_error": None, "last_publication_error": None,
        }

    def start(self):
        with self._lock:
            if self._closing:
                raise RuntimeError("Replay state channel is closed")
            if self._started:
                return self
            self._started = True
            # A Context cannot run concurrently in two threads. Each needs its
            # own copy to inherit Modal's InputContext without sharing changes.
            control_context = contextvars.copy_context()
            publication_context = contextvars.copy_context()
            self._control_thread = Thread(
                target=control_context.run, args=(self._read_loop,),
                name="replay-state-control", daemon=True,
            )
            self._publication_thread = Thread(
                target=publication_context.run, args=(self._publish_loop,),
                name="replay-state-publication", daemon=True,
            )
            self._control_thread.start()
            self._publication_thread.start()
        return self

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()

    def _read_loop(self):
        while not self._stop.is_set():
            started = time.monotonic()
            with self._lock:
                self._read_started = started
            error = None
            value = None
            try:
                raw = self._reader()
                if raw is not None and type(raw) is not dict:
                    raise TypeError("Replay control must be a primitive dictionary or None")
                value = _primitive_copy(raw)
            except Exception as exc:  # noqa: BLE001 -- keep only the class, never source credentials.
                error = type(exc).__name__
            elapsed = time.monotonic() - started
            with self._lock:
                self._read_started = None
                self._metrics["control_reads"] += 1
                self._metrics["control_seconds"] += elapsed
                if error is None:
                    self._control = value
                    self._initial.set()
                else:
                    self._metrics["control_errors"] += 1
                    self._metrics["last_control_error"] = error
                    # Preserve the last control and ORIGINAL expires_at. A read
                    # failure must neither extend the lease nor predict time.
            self._stop.wait(max(0.0, self.poll_seconds - elapsed))

    def _publish_loop(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending is not None or self._closing)
                if self._closing and (not self._flushlatest or self._pending is None):
                    return
                payload, self._pending = self._pending, None
                self._inflight = True
                self._publication_started = time.monotonic()
                self._metrics["max_inflight"] = 1
            started = time.monotonic()
            error = None
            try:
                self._publisher(payload)
            except Exception as exc:  # noqa: BLE001 -- SDK exception text may contain private URLs.
                error = type(exc).__name__
            elapsed = time.monotonic() - started
            with self._condition:
                self._inflight = False
                self._publication_started = None
                self._metrics["publications"] += 1
                self._metrics["publication_seconds"] += elapsed
                if error is not None:
                    self._metrics["publication_errors"] += 1
                    self._metrics["last_publication_error"] = error
                self._condition.notify_all()

    def wait_initial(self, timeout=5):
        timeout = _timeout(timeout, "timeout")
        if not self._initial.wait(timeout):
            raise TimeoutError("Initial replay control was not available before timeout")
        return self.control()

    def control(self):
        with self._lock:
            return _primitive_copy(self._control)

    def publish(self, snapshot):
        if type(snapshot) is not dict:
            raise TypeError("Replay publication must be a primitive dictionary")
        payload = _primitive_copy(snapshot)
        with self._condition:
            if self._closing:
                raise RuntimeError("Replay state channel is closed")
            if self._pending is not None:
                self._metrics["coalesced_snapshots"] += 1
            self._pending = payload
            self._metrics["max_pending"] = 1
            self._condition.notify()

    @property
    def stats(self):
        with self._lock:
            now = time.monotonic()
            return {
                **self._metrics, "pending": int(self._pending is not None),
                "inflight": int(self._inflight), "closed": self._closing,
                "control_inflight_seconds": (
                    now - self._read_started if self._read_started is not None else 0.0
                ),
                "publication_inflight_seconds": (
                    now - self._publication_started if self._publication_started is not None else 0.0
                ),
                "control_thread_alive": bool(self._control_thread and self._control_thread.is_alive()),
                "publication_thread_alive": bool(self._publication_thread and self._publication_thread.is_alive()),
            }

    def close(self, timeout=1, flushlatest=True):
        timeout = _timeout(timeout, "timeout")
        if type(flushlatest) is not bool:
            raise TypeError("flushlatest must be boolean")
        deadline = time.monotonic() + timeout
        with self._condition:
            if not self._closing:
                self._closing, self._flushlatest = True, flushlatest
                if not flushlatest:
                    self._pending = None
                self._stop.set()
                self._condition.notify_all()
        for thread in (self._control_thread, self._publication_thread):
            if thread is not None and thread is not current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
