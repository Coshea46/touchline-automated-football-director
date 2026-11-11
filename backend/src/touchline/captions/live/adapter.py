"""Bounded IPC adapter; the web backend never imports the ElevenLabs SDK."""

import json
import os
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from threading import Condition, Thread

from touchline.captions.live.events import MAX_CAPTION_EVENT_BYTES
from touchline.captions.limits import DEFAULT_AUDIO_SECONDS, DEFAULT_COST_USD, validate_limits
from touchline.captions.identity import canonical_json
from touchline.paths import BACKEND_ROOT, PROJECT_ROOT

MAX_CONTROL_BYTES = 24576
MAX_EVENT_BYTES = MAX_CAPTION_EVENT_BYTES


class RealtimeCaptionWorker:
    def __init__(self, callback, *, python_executable=None, max_audio_seconds=DEFAULT_AUDIO_SECONDS,
                 max_cost_usd=DEFAULT_COST_USD, env=None, process_factory=subprocess.Popen):
        validate_limits(max_audio_seconds, max_cost_usd)
        self.callback = callback
        self.env = {**os.environ, **(env or {})}
        default = BACKEND_ROOT / ".venv-captions" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        executable = Path(python_executable or self.env.get("MATCHPULSE_CAPTION_PYTHON") or default).expanduser()
        self.python = str(executable if executable.is_absolute() else PROJECT_ROOT / executable)
        self.env["MATCHPULSE_CAPTION_MAX_AUDIO_SECONDS"] = str(max_audio_seconds)
        self.env["MATCHPULSE_CAPTION_BUDGET_USD"] = str(max_cost_usd)
        self.env.setdefault("MATCHPULSE_PROJECT_ROOT", str(PROJECT_ROOT))
        self.root, self._factory = PROJECT_ROOT, process_factory
        self._condition, self._latest, self._revision = Condition(), None, 0
        self._closed, self._writer, self._reader, self._process = False, None, None, None
        self._close_confirmed = None
        self._terminal_reason, self._launching = None, False
        self._metrics = {"control_updates": 0, "max_pending_controls": 0, "processes_started": 0}

    @property
    def stats(self):
        with self._condition:
            return dict(self._metrics)

    def start(self):
        with self._condition:
            if self._closed or self._writer is not None:
                return
            self._writer = Thread(target=self._write_controls, name="caption-control", daemon=True)
            self._writer.start()

    def update(self, control):
        if not isinstance(control, dict):
            raise TypeError("Caption control must be a primitive object")
        encoded = canonical_json(control)
        if len(encoded.encode()) > MAX_CONTROL_BYTES:
            raise ValueError("Caption control exceeds its size limit")
        self.start()
        with self._condition:
            if self._closed:
                return
            self._latest = json.loads(encoded)
            self._revision += 1
            self._metrics["control_updates"] += 1
            self._metrics["max_pending_controls"] = 1
            active = control.get("active", control.get("enabled", False))
            if active and self._terminal_reason is None:
                if self._writer is not None and not self._writer.is_alive():
                    self._terminal_reason = "caption_worker_unavailable"
                elif self._process is not None:
                    poll = getattr(self._process, "poll", None)
                    if callable(poll) and poll() is not None:
                        self._terminal_reason = "caption_worker_ended"
            reason = self._terminal_reason
            if reason is None:
                self._condition.notify_all()
        # An explicit Off -> On may retry the UI, but cannot resurrect a dead
        # process or renew its budget. Report setup failure for the NEW identity.
        if active and reason is not None:
            self._error(control, reason)

    def stop(self, reason="paused"):
        with self._condition:
            control = dict(self._latest or {})
        control.update(active=False, enabled=False, reason=str(reason)[:80])
        self.update(control)

    def _error(self, control, reason):
        event = {key: control.get(key) for key in ("session_id", "match_id", "source_key", "source_identity",
                                                  "generation", "clock_epoch")}
        event.update(type="caption", state="unavailable", reason=reason, text="",
                     provider="elevenlabs", model="scribe_v2_realtime")
        self._deliver(event)

    def _deliver(self, event):
        try:
            if isinstance(event.get("metrics"), dict):
                with self._condition:
                    self._metrics.update(event["metrics"])
            if event.get("state") in ("error", "unavailable"):
                reason = event.get("reason")
                if isinstance(reason, str) and reason.replace("_", "").isalnum() and len(reason) <= 80:
                    with self._condition:
                        self._metrics["last_error_reason"] = reason
            self.callback(event)
        except Exception:  # noqa: BLE001 -- callbacks must never leak credentials
            with self._condition:
                self._metrics["callback_errors"] = self._metrics.get("callback_errors", 0) + 1

    def _launch(self):
        if not Path(self.python).is_file():
            raise FileNotFoundError("Caption runtime is not installed")
        options = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
                   "text": True, "encoding": "utf-8", "bufsize": 1, "cwd": str(self.root), "env": self.env}
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        with self._condition:
            if self._closed:
                return False
            self._launching = True
        try:
            process = self._factory([self.python, "-B", "-m", "touchline.captions.live.worker"], **options)
        except BaseException:
            with self._condition:
                self._launching = False
            raise
        with self._condition:
            self._process = process
            self._launching = False
            self._metrics["processes_started"] += 1
            closed = self._closed
        if closed:
            # close() may race a slow native spawn. The child has received NO
            # control/audio and must be retired before any active input write.
            with suppress(OSError, ValueError):
                process.stdin.close()
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    with suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=.5)
            return False
        self._reader = Thread(target=self._read_events, name="caption-events", daemon=True)
        self._reader.start()
        return True

    def _write_controls(self):
        seen = 0
        while True:
            with self._condition:
                self._condition.wait_for(lambda seen=seen: self._closed or self._revision != seen)
                if self._closed:
                    return
                control, seen = dict(self._latest), self._revision
            if self._process is None and not control.get("active", control.get("enabled", False)):
                continue
            try:
                if self._process is None and not self._launch():
                    return
                with self._condition:
                    if self._closed or self._terminal_reason is not None:
                        return
                self._process.stdin.write(canonical_json(control) + "\n")
                self._process.stdin.flush()
            except (OSError, ValueError) as exc:
                reason = "caption_runtime_missing" if isinstance(exc, FileNotFoundError) else "caption_worker_unavailable"
                with self._condition:
                    self._terminal_reason = reason
                self._error(control, reason)
                return  # no automatic paid restart

    def _read_events(self):
        reason = "caption_worker_ended"
        while not self._closed:
            try:
                line = self._process.stdout.readline(MAX_EVENT_BYTES + 1)
                if not line:
                    break
                if len(line.encode()) > MAX_EVENT_BYTES:
                    reason = "caption_worker_protocol_error"
                    break
                event = json.loads(line)
                if isinstance(event, dict) and event.get("type") == "caption":
                    self._deliver(event)
                else:
                    reason = "caption_worker_protocol_error"
                    break
            except (OSError, ValueError):
                reason = "caption_worker_protocol_error"
                break
        if not self._closed:
            with self._condition:
                self._terminal_reason = reason
                control = dict(self._latest or {})
            # A malformed/closed event pipe is irrecoverable even when its
            # child still runs. Retire that owned process now; otherwise a
            # failed writer could prevent Off from revoking its provider lease.
            poll = getattr(self._process, "poll", None)
            if callable(poll) and poll() is None:
                with suppress(OSError):
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=.5)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        with suppress(subprocess.TimeoutExpired):
                            self._process.wait(timeout=.5)
            if control.get("active", control.get("enabled", False)):
                self._error(control, reason)

    def close(self):
        with self._condition:
            if self._closed:
                return self._close_confirmed is True
            self._closed = True
            self._condition.notify_all()
            process, launching = self._process, self._launching
        confirmed = process is None and not launching
        if process is not None:
            def close_input():
                with suppress(OSError, ValueError):
                    process.stdin.close()
            Thread(target=close_input, daemon=True).start()
            try:
                confirmed = process.wait(timeout=1.5) == 0
            except (OSError, ValueError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=.5)
        if self._writer:
            self._writer.join(timeout=.2)
        if self._reader:
            self._reader.join(timeout=.2)
        self._close_confirmed = confirmed
        return confirmed
