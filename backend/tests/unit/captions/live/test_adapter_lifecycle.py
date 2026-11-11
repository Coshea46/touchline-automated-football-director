"""Offline tests for unit captions live test_adapter_lifecycle."""


import json
import sys
import time
from io import StringIO
from threading import Event
from touchline.captions.live.adapter import RealtimeCaptionWorker


def wait_until(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.005)


def active(generation):
    return {"active": True, "generation": generation, "session_id": "fixture", "match_id": "cam-1",
            "source_key": "match-1.mp4", "source_identity": "a" * 64, "clock_epoch": 0}


def test_missing_runtime_off_on_fails_fast_for_new_generation_without_spawn(tmp_path):
    events = []
    worker = RealtimeCaptionWorker(events.append, python_executable=tmp_path / "not-installed.exe")
    worker.update(active(1))
    wait_until(lambda: events)
    assert events[-1]["reason"] == "caption_runtime_missing"
    worker.stop("viewer_disabled")
    count = len(events)
    worker.stop("viewer_disabled")
    assert len(events) == count
    worker.update(active(3))
    assert events[-1]["generation"] == 3 and events[-1]["state"] == "unavailable"
    assert events[-1]["reason"] == "caption_runtime_missing"
    assert worker.stats["processes_started"] == 0
    assert worker.close() is True


def test_child_exit_explicit_on_returns_unavailable_without_process_restart():
    events, starts = [], []
    class Process:
        def __init__(self, *args, **kwargs):
            starts.append(True)
            self.stdin, self.stdout = StringIO(), StringIO("")

        def wait(self, timeout):
            return 0

        def poll(self):
            return 0
    worker = RealtimeCaptionWorker(events.append, python_executable=sys.executable, process_factory=Process)
    worker.update(active(1))
    wait_until(lambda: events)
    worker.stop()
    worker.update(active(2))
    assert events[-1]["generation"] == 2 and events[-1]["reason"] == "caption_worker_ended"
    assert len(starts) == 1
    assert worker.close() is True


def test_close_racing_spawn_cannot_send_old_active_control_or_claim_confirmation():
    entered, release, retired = Event(), Event(), Event()
    processes = []
    class Input:
        def __init__(self):
            self.writes = []

        def write(self, value):
            self.writes.append(json.loads(value))

        def flush(self):
            pass

        def close(self):
            retired.set()
    class Process:
        def __init__(self):
            self.stdin, self.stdout = Input(), StringIO("")

        def wait(self, timeout):
            return 0
    def delayed_spawn(*args, **kwargs):
        entered.set()
        release.wait(2)
        process = Process()
        processes.append(process)
        return process
    worker = RealtimeCaptionWorker(lambda _: None, python_executable=sys.executable, process_factory=delayed_spawn)
    worker.update(active(1))
    assert entered.wait(1)
    assert worker.close() is False
    release.set()
    assert retired.wait(1)
    assert processes[0].stdin.writes == []


def test_irrecoverable_event_pipe_retires_child_before_reporting_unavailable():
    events, retired, starts = [], Event(), []
    class Process:
        def __init__(self, *args, **kwargs):
            self.stdin, self.stdout = StringIO(), StringIO("invalid event\n")
            self.alive = True
            starts.append(True)

        def poll(self):
            return None if self.alive else 1

        def terminate(self):
            self.alive = False
            retired.set()

        def wait(self, timeout):
            return 1
    worker = RealtimeCaptionWorker(events.append, python_executable=sys.executable, process_factory=Process)
    worker.update(active(1))
    wait_until(lambda: events)
    assert retired.is_set() and events[-1]["reason"] == "caption_worker_protocol_error"
    worker.stop()
    worker.update(active(2))
    assert events[-1]["generation"] == 2 and len(starts) == 1
    assert worker.close() is False
