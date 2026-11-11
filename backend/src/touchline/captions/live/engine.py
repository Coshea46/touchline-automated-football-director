"""Live caption engine, retaining the bounded focused-audio contract."""

import asyncio
import base64
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from urllib.parse import urlsplit
from touchline.captions.live.events import MAX_CAPTION_WORD_CHARS, bounded_caption_event, compact_transcript
from touchline.captions.limits import DEFAULT_AUDIO_SECONDS, DEFAULT_COST_USD, LIVE_PRICE_PER_HOUR_USD, validate_limits
from touchline.captions.errors import CaptionError
from touchline.captions.identity import verify_source_identity


from .connection import LIVE_MODEL, PROVIDER_ERROR_CODES, official_connect

def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)

def valid_control(control, wall_now=time.time):
    if not isinstance(control, dict) or not control.get("active", control.get("enabled", False)):
        return False
    if (not control.get("playing") or control.get("stalled") or control.get("available") is False
            or not _finite(control.get("expires_at")) or control["expires_at"] <= wall_now()
            or not _finite(control.get("deadline")) or control["deadline"] <= wall_now()
            or any(not _finite(control.get(name)) or control[name] < 0 for name in ("position", "offset_seconds"))
            or type(control.get("generation")) is not int or control["generation"] < 0
            or type(control.get("clock_epoch")) is not int or control["clock_epoch"] < 0
            or not re.fullmatch(r"[a-f0-9]{64}", str(control.get("source_identity", "")))):
        return False
    try:
        parsed = urlsplit(control.get("source_url", ""))
        return (parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username
                and not parsed.password and len(control["source_url"]) <= 16384)
    except (TypeError, ValueError):
        return False

class RealtimeEngine:
    def __init__(self, control_reader, publish, *, max_audio_seconds=DEFAULT_AUDIO_SECONDS,
                 max_cost_usd=DEFAULT_COST_USD,
                 api_key_present=False, audio_factory=None,
                 connect_factory=official_connect, identity_checker=verify_source_identity,
                 now=time.monotonic, wall_now=time.time):
        validate_limits(max_audio_seconds, max_cost_usd)
        if audio_factory is None:
            from touchline.captions.live.audio import ProgressiveAudioReader
            audio_factory = ProgressiveAudioReader
        self.control_reader, self.publish = control_reader, publish
        self.audio_factory, self.connect_factory, self.identity_checker = audio_factory, connect_factory, identity_checker
        self.api_key_present = api_key_present
        self.max_audio, self.max_cost = max_audio_seconds, max_cost_usd
        self.now, self.wall_now = now, wall_now
        self.closed = False
        self.metrics = {"provider_sessions": 0, "provider_connected_seconds": 0.0, "pcm_seconds": 0.0,
                        "estimated_cost_usd": 0.0, "errors": 0, "generation_changes": 0,
                        "forced_provider_closes": 0,
                        "provider_session_started": False, "provider_close_code": None,
                        "transcript_compactions": 0, "last_provider_text_chars": 0,
                        "last_provider_word_count": 0, "transcript_events_dropped": 0,
                        "first_transcript_latency_seconds": None}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="caption-audio")
        self._connected_since = None
        self._budget_exhausted = False
        self._deadline = None
        self._identity_cache = {}

    def _totals(self):
        values = dict(self.metrics)
        if self._connected_since is not None:
            values["provider_connected_seconds"] += max(0, self.now() - self._connected_since)
        values["estimated_cost_usd"] = values["provider_connected_seconds"] / 3600 * LIVE_PRICE_PER_HOUR_USD
        return values

    def emit(self, control, state, *, _transcript_compacted=False, **extra):
        event = {key: control.get(key) for key in ("session_id", "match_id", "source_key", "source_identity",
                                                  "generation", "clock_epoch")}
        event.update(type="caption", provider="elevenlabs", model=LIVE_MODEL, state=state, text="",
                     received_at=self.wall_now(), metrics=self._totals())
        event.update(extra)
        bounded = bounded_caption_event(event)
        if bounded is None:
            self.metrics['transcript_events_dropped'] += 1
            return
        if state in ('partial', 'committed') and (_transcript_compacted
                or bounded.get('text') != event.get('text') or bounded.get('words') != event.get('words')):
            self.metrics['transcript_compactions'] += 1
            bounded['metrics'] = self._totals()
            bounded = bounded_caption_event(bounded)
            if bounded is None:
                self.metrics['transcript_events_dropped'] += 1
                return
        self.publish(bounded)

    @staticmethod
    def _token(control):
        return tuple(control.get(key) for key in ("session_id", "generation", "match_id", "clock_epoch", "source_url"))

    def _current(self, token):
        control = self.control_reader()
        if self._deadline is not None and self.wall_now() >= self._deadline:
            return None
        return control if valid_control(control, self.wall_now) and self._token(control) == token else None

    def _over_budget(self):
        totals = self._totals()
        return (totals["pcm_seconds"] >= self.max_audio - 1e-7
                or totals["provider_connected_seconds"] >= self.max_audio
                or totals["estimated_cost_usd"] >= self.max_cost)

    async def run(self):
        task, handled = None, set()
        try:
            while not self.closed:
                control = self.control_reader()
                token = self._token(control) if isinstance(control, dict) else None
                active = valid_control(control, self.wall_now)
                if active:
                    candidate = min(control["deadline"], self.wall_now() + self.max_audio)
                    self._deadline = candidate if self._deadline is None else min(self._deadline, candidate)
                nearing_deadline = self._deadline is not None and self._deadline - self.wall_now() <= 1.1
                if self._deadline is not None and self.wall_now() >= self._deadline:
                    self._budget_exhausted = True
                    active = False
                if self._connected_since is not None and (nearing_deadline or
                        self._totals()["provider_connected_seconds"] >= min(
                            self.max_audio, self.max_cost * 3600 / LIVE_PRICE_PER_HOUR_USD) - 1.1
                        or self._totals()["estimated_cost_usd"] >= self.max_cost):
                    self._budget_exhausted = True
                    active = False
                if task and (not active or token != task.caption_token):
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    task = None
                if active and token not in handled and not self._budget_exhausted:
                    handled.add(token)
                    self.metrics["generation_changes"] += 1
                    task = asyncio.create_task(self._stream(dict(control)))
                    task.caption_token = token
                elif task and task.done():
                    with suppress(asyncio.CancelledError):
                        await task
                    task = None
                await asyncio.sleep(.03)
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            self._executor.shutdown(wait=False, cancel_futures=True)

    async def _stream(self, initial):
        token = self._token(initial)
        connection, reader, opening = None, None, None
        first_submit, last_report_at, anchor, sent_end = None, None, None, None
        failed = asyncio.Event()
        startup_seen = asyncio.Event()
        error_reason = ["provider_error"]
        finish_state, finish_reason = "paused", "generation_finished"
        loop = asyncio.get_running_loop()
        try:
            if not self.api_key_present:
                finish_state, finish_reason = "unavailable", "api_key_missing"
                return
            if self.metrics["forced_provider_closes"]:
                finish_state, finish_reason = "unavailable", "cleanup_unconfirmed"
                return
            if self._over_budget():
                finish_state, finish_reason = "stopped", "budget_exhausted"
                return
            self.emit(initial, "connecting")
            cache_key = (initial["source_key"], initial["source_url"], initial["source_identity"])
            cached = self._identity_cache.get(cache_key)
            if cached and cached[0] > self.now():
                identity = cached[1]
            else:
                identity = await loop.run_in_executor(self._executor, self.identity_checker,
                                                       initial["source_key"], initial["source_url"])
                self._identity_cache[cache_key] = (self.now() + 300, identity)
                if len(self._identity_cache) > 16:
                    self._identity_cache.pop(next(iter(self._identity_cache)))
            if identity != initial["source_identity"]:
                finish_state = "unavailable"
                finish_reason = "source_identity_changed" if identity else "source_identity_verification_unavailable"
                return
            latest = self._current(token)
            if not latest:
                return
            # Open only at the actually reported source point, never five or
            # thirty seconds ahead. The single executor bounds decode work.
            opening = self._executor.submit(self.audio_factory, latest["source_url"],
                                             latest["offset_seconds"] + latest["position"])
            reader = await asyncio.shield(asyncio.wrap_future(opening))
            if not self._current(token):
                return
            self._connected_since = self.now()
            remaining = min(self.max_audio - self.metrics["provider_connected_seconds"],
                            min(initial["deadline"], self._deadline or initial["deadline"]) - self.wall_now(),
                            (self.max_cost - self._totals()["estimated_cost_usd"]) * 3600 / LIVE_PRICE_PER_HOUR_USD)
            if remaining <= 1.1:
                finish_state, finish_reason = "stopped", "budget_exhausted"
                return
            connection = await asyncio.wait_for(self.connect_factory(initial), remaining - 1.1)
            self.metrics["provider_sessions"] += 1

            def transcript(state, value, timestamps=False):
                if not self._current(token) or failed.is_set() or not isinstance(value, dict):
                    return
                text = value.get("text", "") if isinstance(value, dict) else ""
                if not isinstance(text, str):
                    return
                value = compact_transcript(value)
                self.metrics['last_provider_text_chars'] = value['_provider_text_chars']
                self.metrics['last_provider_word_count'] = value['_provider_word_count']
                extra = {"text": value['text'], "source_anchor_seconds": anchor,
                         "observed_available_at": last_report_at, "observation_end": sent_end}
                if self.metrics["first_transcript_latency_seconds"] is None and text and first_submit is not None:
                    self.metrics["first_transcript_latency_seconds"] = max(0, self.now() - first_submit)
                if timestamps and anchor is not None:
                    words = []
                    for word in value.get("words", []):
                        if not isinstance(word, dict):
                            continue
                        a, b = word.get("start"), word.get("end")
                        if (not _finite(a) or not _finite(b) or not 0 <= a <= b
                                or sent_end is None or anchor + b > sent_end + 1 / 16000
                                or not isinstance(word.get("text"), str)
                                or len(word['text']) > MAX_CAPTION_WORD_CHARS):
                            continue
                        words.append({"text": word["text"], "start_seconds": anchor + a,
                                      "end_seconds": anchor + b, "type": word.get("type", "word")})
                    extra["words"] = words
                self.emit(initial, state, _transcript_compacted=value['_transcript_compacted'], **extra)

            def provider_error(value):
                kind = value.get("message_type", "provider_error") if isinstance(value, dict) else "provider_error"
                error_reason[0] = kind if isinstance(kind, str) and kind in PROVIDER_ERROR_CODES else "provider_error"
                failed.set()
                startup_seen.set()

            def provider_started(_):
                self.metrics["provider_session_started"] = True
                startup_seen.set()

            def provider_closed(value=None):
                code = value.get("code") if isinstance(value, dict) else None
                self.metrics["provider_close_code"] = code if type(code) is int and 1000 <= code <= 4999 else None
                failed.set()
                startup_seen.set()

            connection.on("partial_transcript", lambda value: transcript("partial", value))
            connection.on("committed_transcript", lambda value: transcript("committed", value))
            connection.on("committed_transcript_with_timestamps", lambda value: transcript("committed", value, True))
            connection.on("error", provider_error)
            connection.on("close", provider_closed)
            connection.on("session_started", provider_started)
            if getattr(connection, "requires_session_started", False):
                await asyncio.wait_for(startup_seen.wait(), min(5, max(.01, initial["deadline"] - self.wall_now() - 1.1)))
            if failed.is_set():
                raise CaptionError(error_reason[0])
            if not self._current(token):
                return
            self.emit(initial, "listening", source_anchor_seconds=initial["offset_seconds"] + initial["position"])
            while True:
                control = self._current(token)
                if not control:
                    break
                if failed.is_set():
                    raise CaptionError(error_reason[0])
                if self._over_budget():
                    finish_state, finish_reason = "stopped", "budget_exhausted"
                    break
                permitted = control["offset_seconds"] + control["position"]
                remaining = max(0, self.max_audio - self.metrics["pcm_seconds"])
                chunk = await loop.run_in_executor(self._executor, reader.pull, permitted, min(.25, remaining))
                # Decode may have blocked while source/generation was revoked.
                # Recheck the original lease before submitting even one byte.
                current = self._current(token)
                if not current or self._over_budget():
                    break
                if failed.is_set():
                    raise CaptionError(error_reason[0])
                if chunk:
                    if chunk.end_seconds > current["offset_seconds"] + current["position"] + 1e-7:
                        raise CaptionError("audio_future_samples_rejected")
                    if anchor is None:
                        anchor, first_submit = chunk.start_seconds, self.now()
                    if sent_end is not None and abs(chunk.start_seconds - sent_end) > 1.01 / 16000:
                        raise CaptionError("audio_submission_discontinuity")
                    last_report_at = current.get("report_received_at")
                    sent_end = chunk.end_seconds
                    self.metrics["pcm_seconds"] += len(chunk.pcm) / 32000
                    await asyncio.wait_for(connection.send({"audio_base_64": base64.b64encode(chunk.pcm).decode(),
                                                            "sample_rate": 16000}), 2)
                await asyncio.sleep(.03)
        except EOFError:
            finish_state, finish_reason = "stopped", "source_eof"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- provider/decoder errors must never disclose credentials
            self.metrics["errors"] += 1
            finish_state = "error"
            finish_reason = (error_reason[0] if failed.is_set() else
                             exc.code if isinstance(exc, CaptionError) else f"caption_{type(exc).__name__}").lower()
        finally:
            if connection is not None:
                try:
                    await asyncio.wait_for(connection.close(), 1)
                except Exception:  # noqa: BLE001 -- fail closed on every SDK cleanup failure
                    self.metrics["forced_provider_closes"] += 1
                    finish_state, finish_reason = "error", "cleanup_unconfirmed"
                    transport = getattr(getattr(connection, "websocket", None), "transport", None)
                    if transport:
                        transport.abort()
                http = getattr(connection, "_caption_http_pool", None)
                if http:
                    http.close()
            if self._connected_since is not None:
                self.metrics["provider_connected_seconds"] += max(0, self.now() - self._connected_since)
                self._connected_since = None
            if reader is None and opening is not None:
                def close_opened(future):
                    if not future.cancelled() and future.exception() is None:
                        # This concurrent-future callback runs on the decoder
                        # thread, including after executor shutdown. No late
                        # submission can orphan a just-opened media container.
                        future.result().close()
                opening.cancel()
                opening.add_done_callback(close_opened)
            elif reader is not None:
                loop.run_in_executor(self._executor, reader.close)
            if self._budget_exhausted:
                finish_state, finish_reason = "stopped", "budget_exhausted"
            self.emit(initial, finish_state, reason=finish_reason)
