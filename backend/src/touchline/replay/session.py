"""Nonblocking replay lifecycle, viewer lease and compact-score orchestration."""

import asyncio
import logging
import math
import time
import uuid

from touchline.replay.director import Director
from touchline.replay.sources import number

from touchline.captions.limits import DEFAULT_AUDIO_SECONDS, DEFAULT_COST_USD, MAX_AUDIO_SECONDS, MAX_COST_USD
from touchline.ml_inference.streaming.contract import MAX_SESSION_BUDGET_USD

from touchline.replay.config import SessionConfig
from touchline.replay.diagnostics import worker_diagnostics, transient_transport_error
from touchline.replay.captions import CaptionCoordinator
from touchline.replay.monitoring import MonitoringCoordinator

logger = logging.getLogger(__name__)


class ReplaySession:
    """One shared replay session; the first connected viewer owns its clock.

    SDK work runs outside socket handlers in two sequential background loops.
    There is at most one score read and one control/lifecycle RPC in flight.
    The worker receives the last reported media position, never extrapolation.
    """

    def __init__(self, registry, *, config=None, bridge=None, publish=None,
                 now=time.time, monotonic=time.monotonic, session_id=None,
                 caption_worker=None, caption_max_audio_seconds=DEFAULT_AUDIO_SECONDS,
                 caption_max_cost_usd=DEFAULT_COST_USD, prepared_config=None,
                 bridge_factory=None, preparation_error=None):
        self.captions = CaptionCoordinator(self)
        self.monitoring = MonitoringCoordinator(self)
        self.registry, self.config, self.bridge = registry, config or SessionConfig(), bridge
        self.publish = publish or (lambda payload: None)
        self.now, self.monotonic = now, monotonic
        self.session_id = session_id or uuid.uuid4().hex
        self.sequence = 0
        self.sources = ()
        self.director = None
        self.clients = []
        self.controller = None
        self._controller_report_pending = False
        self.position, self.playing, self.rate, self.stalled = 0.0, False, 1.0, False
        self.clock_epoch = 0
        self.state = "waiting" if self.config.monitor else "disabled"
        self.error_code = None
        self.last_report_at = None
        self._paused_at = None
        self._ready_at = None
        self._monitor_started_at = None
        self._handle = None
        self._stopping_handle = None
        self._start_inflight = None
        self._worker_ready = False
        self._stop_requested = False
        self._restart_after_seek = False
        self._closing = False
        self._closed = False
        self._ended = False
        self._finished = False
        self._finished_reason = "session_limit_reached"
        self._restarts = 0
        self._retry_at = 0.0
        self._sources_changed = True
        self._source_refresh_retry_at = 0.0
        self._source_refresh_failures = 0
        self._source_refresh_task = None
        self._source_refresh_future = None
        self._source_refresh_executor = None
        self._source_cleanup_future = None
        self._source_cleanup_waiter = None
        self._tasks = []
        self._last_recommendation = None
        self._last_ack_sequence = None
        self._unplayable = set()
        self._reserved_compute_usd = 0.0
        self._selection_pending_until = 0.0
        if prepared_config is not None and (not isinstance(prepared_config, SessionConfig)
                or not prepared_config.monitor or prepared_config.mode != 'real'
                or prepared_config.max_duration_seconds > 3600 or prepared_config.max_estimated_cost_usd > MAX_SESSION_BUDGET_USD
                or prepared_config.max_restarts != 0):
            raise ValueError(f'Prepared real monitoring requires a bounded 3600-second/${MAX_SESSION_BUDGET_USD} service allowance')
        self._prepared_config, self._bridge_factory = prepared_config, bridge_factory
        self._preparation_error = (preparation_error if preparation_error in
            ('credentials_unavailable', 'not_prepared') else None)
        self._activation_requested = False
        self._activation_inflight = False
        self._start_authorized = bool(prepared_config is not None and self.config.monitor)
        number(caption_max_audio_seconds, "caption_max_audio_seconds", minimum=1, maximum=MAX_AUDIO_SECONDS)
        number(caption_max_cost_usd, "caption_max_cost_usd", minimum=0.0001, maximum=MAX_COST_USD)
        self.caption_worker = caption_worker
        self._caption_configured = caption_worker is not None
        self.caption_max_audio_seconds = caption_max_audio_seconds
        self.caption_max_cost_usd = caption_max_cost_usd
        self._caption_loop_owner = None
        self._caption_user_enabled = True
        self._caption_enabled = caption_worker is not None
        self._caption_generation = 0
        self._caption_status = "paused" if caption_worker is not None else "unavailable"
        self._caption_reason = "awaiting_playback" if caption_worker is not None else "missing_key"
        self._caption_deadline = None
        self._caption_report_received_at = None
        self._caption_report_sequence = 0
        self._caption_actual_match_id = None
        self._caption_active_identity = None
        self._caption_pending_match_id = None
        self._caption_pending_until = None
        self._caption_preparing_report_sequence = None
        self.metrics = {"recommendations": 0, "director_seconds": 0.0, "worker_failures": 0,
                        "control_rpc_count": 0, "control_rpc_seconds": 0.0,
                        "score_rpc_count": 0, "score_rpc_seconds": 0.0,
                        "replay_transport": self.config.replay_transport,
                        "owned_stop_method": ("websocket_graceful" if self.config.replay_transport == "websocket"
                                              else "modal_input_cancel")}
        self.worker_health = {}

    async def initialize(self):
        try:
            self.sources = await asyncio.wrap_future(self.monitoring._source_executor().submit(self.registry.resolve))
            if self.config.gpu_workers is not None and self.config.gpu_workers > len(self.sources):
                raise ValueError("gpu_workers cannot exceed registered source count")
            self.director = Director([source.match_id for source in self.sources],
                                     session_id=self.session_id, model_version=self.config.model_version,
                                     mode=self.config.mode, director_mode=self.config.director_mode,
                                     emergency_enabled=self.config.director_emergency_enabled,
                                     emergency_margin=self.config.director_emergency_margin)
            self._model_health = dict(self.director.healthy)
            if self._prepared_config is not None and not self.config.monitor:
                self.director.set_auto(False)
            if self.config.mock_scores is not None and set(self.config.mock_scores) - set(self.director.match_ids):
                raise ValueError("Mock score configured for an unregistered match")
            if self.config.monitor and self.bridge is None:
                # Default startup never imports or authenticates the Modal SDK.
                if self.config.replay_transport == "websocket":
                    from touchline.ml_inference.streaming.ws_client import ReplayWebSocketClient
                    self.bridge = ReplayWebSocketClient.from_env()
                else:
                    from touchline.ml_inference.streaming.client import ReplayClient
                    self.bridge = ReplayClient()
            return self
        except BaseException:
            # Initialization has no running viewer/director lifecycle yet.
            # Retire native access in order even if the initial resolve was cancelled.
            self._closing = True
            try:
                await self.monitoring.close_sources()
            finally:
                self._closed = True
                self.state = "stopped"
            raise

    @property
    def auto_starting(self):
        return self.monitoring.auto_starting

    def auto_start_status(self):
        return self.monitoring.auto_start_status()

    async def _activate_prepared_monitoring(self):
        return await self.monitoring._activate_prepared_monitoring()

    def _next_sequence(self):
        self.sequence += 1
        return self.sequence

    def controls_enabled(self, client_id):
        return client_id is not None and client_id in self.clients and client_id == self.controller

    @property
    def auto_available(self):
        return self.monitoring.auto_available

    def snapshot(self, client_id=None):
        result = {"type": "session", "protocol_version": 1,
                  "session_id": self.session_id, "sequence": self.sequence,
                  "state": self.state, "mode": self.config.mode,
                  "model_version": self.config.model_version, "server_time": self.now(),
                  "playback": {"position": self.position, "playing": self.playing,
                               "rate": self.rate, "clock_epoch": self.clock_epoch},
                  "matches": [source.public_spec(self.director.healthy[source.match_id])
                              for source in self.sources],
                  "current_match_id": self.director.current_match_id,
                  "can_claim_control": True,
                  "auto_available": self.auto_available,
                  "auto_enabled": self.director.auto_enabled and self.auto_available,
                  "auto_requested": self.director.auto_enabled,
                  "auto_can_start": self.auto_start_status()[0],
                  "auto_start_reason": self.auto_start_status()[1], "auto_starting": self.auto_starting,
                  "manual_hold_until": self.director.manual_hold_until,
                  "director": self.director.state(now=self.now(), playback_position=self.position),
                  "captions": self.caption_state()}
        if client_id is not None:
            result["controls_enabled"] = self.controls_enabled(client_id)
        if self.error_code is not None:
            result["error_code"] = self.error_code
        return result

    def _publish_session(self):
        self._next_sequence()
        self.publish(self.snapshot())

    def caption_state(self):
        return self.captions.caption_state()

    def configure_captions(self, worker, *, configured=None, reason=None):
        return self.captions.configure_captions(worker, configured=configured, reason=reason)

    def caption_event(self, event):
        return self.captions.caption_event(event)

    def _caption_revoke(self, reason, *, status="paused"):
        return self.captions._caption_revoke(reason, status=status)

    def _caption_view(self):
        return self.captions._caption_view()

    def _update_captions(self):
        return self.captions._update_captions()

    def _accept_caption_event(self, event):
        return self.captions._accept_caption_event(event)

    async def _caption_loop(self):
        return await self.captions._caption_loop()

    def _set_state(self, state, error_code=None):
        if self.state != state or self.error_code != error_code:
            self.state, self.error_code = state, error_code
            self._publish_session()

    def connect(self, client_id):
        if client_id not in self.clients:
            self.clients.append(client_id)
        if self.controller is None:
            self.controller = client_id
        self._next_sequence()
        return self.snapshot(client_id)

    def disconnect(self, client_id):
        if client_id in self.clients:
            self.clients.remove(client_id)
        if self.controller == client_id:
            self._caption_revoke("viewer_disconnected", status="stopped")
            self.controller = self.clients[0] if self.clients else None
            self.playing = False
            self.last_report_at = None
            self._stop_requested = True
            self._set_state("waiting" if self.config.monitor and not self._finished else self.state)
            self._publish_session()

    def _error(self, code):
        return {"type": "error", "protocol_version": 1, "session_id": self.session_id,
                "sequence": self._next_sequence(), "code": code}

    def _claim_control(self, client_id):
        if self._closing or self._closed:
            return self._error("session_closed")
        if client_id not in self.clients:
            return self._error("viewer_not_registered")
        if not self.controls_enabled(client_id):
            self.controller = client_id
            self._controller_report_pending = True
            self._last_recommendation = self._last_ack_sequence = None
            self.director.reset_confirmation()
            self._selection_pending_until = 0.0
            self._caption_pending_match_id = self._caption_pending_until = None
            self._caption_preparing_report_sequence = None
            self._caption_report_received_at = None
            self._caption_revoke("control_transferred")
            # Preserve the existing worker's original lease without renewing
            # its permit. Only a fresh report from this owner can resume updates.
        self._publish_session()
        return None

    def receive(self, client_id, message):
        """Validate and update local state only; no decoder, SDK or network I/O."""
        if not isinstance(message, dict) or message.get("session_id") != self.session_id:
            return self._error("session_mismatch")
        if message.get("type") == "claim_control":
            return self._claim_control(client_id)
        if not self.controls_enabled(client_id):
            return self._error("viewer_in_use")
        kind = message.get("type")
        if kind == "ack":
            return self._receive_ack(client_id, message)
        if kind == "playback":
            return self._receive_playback(message)
        if kind == "captions":
            enabled = message.get("enabled")
            if type(enabled) is not bool:
                return self._error("invalid_captions")
            if enabled and self.caption_worker is None:
                self._caption_user_enabled = True
                self._publish_session()
                return self._error("captions_unavailable")
            if enabled and self._caption_deadline is not None and self.now() >= self._caption_deadline:
                self._caption_user_enabled = True
                self._caption_enabled = False
                self._caption_revoke("budget_deadline", status="stopped")
                self._publish_session()
                return self._error("captions_budget_exhausted")
            self._caption_user_enabled = enabled
            if enabled != self._caption_enabled:
                self._caption_enabled = enabled
                if enabled:
                    self._caption_status, self._caption_reason = "paused", "awaiting_playback"
                else:
                    self._caption_revoke("viewer_disabled", status="disabled")
            self._publish_session()
            self._update_captions()
            return None
        if kind == "source_preparing":
            match_id = message.get("match_id")
            if not isinstance(match_id, str) or match_id not in self.director.records:
                return self._error("unknown_match")
            self._caption_pending_match_id, self._caption_pending_until = match_id, None
            self._caption_preparing_report_sequence = self._caption_report_sequence
            self._caption_revoke("source_preparing")
            self._publish_session()
            return None
        if kind == "select":
            match_id = message.get("match_id")
            if not isinstance(match_id, str) or match_id not in self.director.records:
                return self._error("unknown_match")
            if match_id != self.director.current_match_id:
                self._caption_pending_match_id, self._caption_pending_until = match_id, None
                self._caption_revoke("manual_selection")
            self.director.manual_select(match_id, now=self.now(), playback_position=self.position)
            self._selection_pending_until = self.monotonic() + 2
            self._last_recommendation = None
            self._publish_session()
            return None
        if kind == "auto":
            enabled = message.get("enabled")
            if not isinstance(enabled, bool):
                return self._error("invalid_auto")
            if enabled and not self.auto_available:
                if self._prepared_config is None:
                    return self._error("auto_unavailable")
                if not self.auto_starting and self._handle is None:
                    can_start, reason = self.auto_start_status()
                    if not can_start:
                        return self._error('auto_' + reason)
                    self._activation_requested = True
                    self._start_authorized = True
            if not enabled and self._prepared_config is not None and self._start_inflight is None and self._handle is None:
                self._activation_requested = self._start_authorized = False
                self._set_state('disabled')
            self.director.set_auto(enabled)
            if enabled and self._activation_requested:
                self._set_state('starting')
            self._last_recommendation = None
            self._publish_session()
            return None
        if kind == "director_mode":
            try:
                self.director.set_mode(message.get("mode"))
            except (ValueError, TypeError):
                return self._error("invalid_director_mode")
            self._last_recommendation = None
            self._publish_session()
            return None
        return self._error("unknown_message")

    def _valid_recommendation(self, recommendation, *, position=None):
        position = self.position if position is None else position
        return (not self._finished and not self._stop_requested and not self._closing
                and not self._controller_report_pending
                and self.state in ("running", "degraded", "paused")
                and recommendation is not None and recommendation.get("clock_epoch") == self.clock_epoch
                and self.director.healthy.get(recommendation.get("selected_match_id"), False)
                and recommendation.get("valid_until", 0) >= self.now()
                and recommendation.get("observation_end", math.inf) <= position + 0.05
                and position - recommendation.get("observation_end", 0) <= self.director.maximum_playback_lag)

    def _receive_ack(self, client_id, message):
        recommendation = self._last_recommendation
        if (not self._valid_recommendation(recommendation) or type(message.get("sequence")) is not int
                or message.get("sequence") != recommendation["sequence"]
                or message.get("clock_epoch") != self.clock_epoch
                or message.get("match_id") != recommendation["selected_match_id"]):
            return self._error("stale_ack")
        if self.controls_enabled(client_id) and self._last_ack_sequence == recommendation["sequence"]:
            return None
        delivered = max(0, self.now() - recommendation["emitted_at"])
        self.metrics["last_ack"] = {"sequence": recommendation["sequence"],
                                    "match_id": recommendation["selected_match_id"],
                                    "controller": self.controls_enabled(client_id),
                                    "delivery_seconds": delivered,
                                    "observation_to_ack_seconds": recommendation["latency_seconds"] + delivered,
                                    "server_received_at": self.now()}
        if self.controls_enabled(client_id):
            if recommendation["selected_match_id"] != self.director.current_match_id:
                self.director.commit_selection(recommendation["selected_match_id"], now=self.now(),
                    kind=recommendation.get("selection_kind", "automatic"),
                    reason=recommendation.get("selection_reason", "challenger_confirmed"),
                    playback_position=self.position,
                    score=recommendation["scores"].get(recommendation["selected_match_id"]))
                self._publish_session()
            self._last_ack_sequence = recommendation["sequence"]
            self._selection_pending_until = self.monotonic() + 2
        return None

    def _receive_playback(self, message):
        if not isinstance(message.get("match_id"), str) or message.get("match_id") not in self.director.records:
            return self._error("unknown_match")
        try:
            position = number(message.get("position"), "position", maximum=86400)
            rate = number(message.get("rate"), "rate", minimum=0.25, maximum=4)
        except ValueError:
            return self._error("invalid_playback")
        if any(not isinstance(message.get(key, False), bool) for key in
               ("playing", "stalled", "available", "ended")):
            return self._error("invalid_playback")
        self._controller_report_pending = False
        now = self.monotonic()
        previous_health = self.director.healthy.copy()
        # A recommendation can be discarded by the player (expiry, unavailable
        # source, manual hold). Its actual clock remains authoritative. Allow a
        # brief source-transition interval following a manual pick/ACK.
        if (message["match_id"] != self.director.current_match_id
                and now >= self._selection_pending_until
                and message.get("available", True) and not message.get("stalled", False)):
            proposal = self._last_recommendation
            automatic = (self.director.auto_enabled and self._valid_recommendation(proposal, position=position)
                         and message.get("available", True) and not message.get("stalled", False)
                         and proposal["selected_match_id"] == message["match_id"])
            self.director.commit_selection(message["match_id"], now=self.now(),
                kind=proposal.get("selection_kind", "automatic") if automatic else "playback",
                reason=proposal.get("selection_reason") if automatic else "playback_feedback",
                playback_position=position,
                score=proposal["scores"].get(message["match_id"]) if automatic else None)
            self._publish_session()
        jumped = False
        if self.last_report_at is not None:
            elapsed = max(0, now - self.last_report_at)
            movement = position - self.position
            # Independent native players can differ slightly at a warm handoff.
            # Keep the actual destination clock; this small source-change-only
            # tolerance avoids interpreting decode jitter as a user seek.
            handoff = (self._caption_actual_match_id is not None
                       and message['match_id'] != self._caption_actual_match_id
                       and message.get('available', True) and not message.get('stalled', False)
                       and not message.get('ended', False))
            backward_tolerance = 0.15 if handoff else 0.001
            jumped = movement < -backward_tolerance - 1e-9 or movement > max(2, elapsed * max(rate, self.rate) + 1)
        self.position, self.rate = position, rate
        self.stalled = message.get("stalled", False) or not message.get("available", True)
        self.playing = message.get("playing", False) and not self.stalled and not message.get("ended", False)
        self.last_report_at = now
        self._ended = message.get("ended", False)
        if message.get("available") is False:
            self._unplayable.add(message["match_id"])
            self.director.healthy[message["match_id"]] = False
        elif message.get("available") is True:
            self._unplayable.discard(message["match_id"])
            self.director.healthy[message["match_id"]] = self._model_health[message["match_id"]]
        if self.playing:
            self._paused_at = None
        elif self._paused_at is None:
            self._paused_at = now
        if jumped:
            self._caption_revoke("seek")
            self.clock_epoch += 1
            self.director.reset(self.clock_epoch)
            self._last_recommendation = None
            partial_eof = (self._handle is not None and not self._finished
                           and any(value.get("status") in ("eof", "stopped")
                                   for value in self.worker_health.values()))
            if partial_eof:
                # An ended WebSocket shard cannot observe a new epoch. Restart
                # the whole owned group, including still-active sibling shards.
                self._stop_requested = True
                self._restart_after_seek = True
                self._worker_ready = False
                self._set_state("starting")
                self._publish_session()
            else:
                self._publish_session()
        elif previous_health != self.director.healthy:
            self._publish_session()
        self._caption_actual_match_id = message["match_id"]
        self._caption_report_received_at = self.now()
        self._caption_report_sequence += 1
        self._update_captions()
        return None

    def control(self, *, active=True):
        return self.monitoring.control(active=active)

    def worker_spec(self):
        return self.monitoring.worker_spec()

    async def _stop_worker(self):
        return await self.monitoring._stop_worker()

    async def _control_rpc(self, control):
        return await self.monitoring._control_rpc(control)

    async def _score_rpc(self):
        return await self.monitoring._score_rpc()

    async def _start_worker(self):
        return await self.monitoring._start_worker()

    async def _control_tick(self):
        return await self.monitoring._control_tick()

    async def _control_loop(self):
        return await self.monitoring._control_loop()

    def accept_snapshot(self, snapshot):
        return self.monitoring.accept_snapshot(snapshot)

    def _evaluate_director(self):
        return self.monitoring._evaluate_director()

    async def _score_loop(self):
        return await self.monitoring._score_loop()

    def start(self):
        if not self._tasks:
            self._caption_loop_owner = asyncio.get_running_loop()
            self._tasks = [asyncio.create_task(self._control_loop(), name="replay-control"),
                           asyncio.create_task(self._score_loop(), name="replay-scores")]
            if self.caption_worker is not None:
                try:
                    self.caption_worker.start()
                    self._tasks.append(asyncio.create_task(self._caption_loop(), name="replay-captions"))
                except Exception:  # noqa: BLE001
                    self.caption_worker = None
                    self._caption_enabled = False
                    self._caption_status, self._caption_reason = "unavailable", "runtime_unavailable"

    async def close(self):
        if self._closed:
            return
        closing_started = time.perf_counter()
        self._closing = True
        self._caption_enabled = False
        self._caption_revoke("session_closed", status="stopped")
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self.caption_worker is not None:
            try:
                confirmed = await asyncio.wait_for(asyncio.to_thread(self.caption_worker.close), timeout=4)
            except Exception:  # noqa: BLE001 - close failures remain sanitized and bounded
                self.metrics["caption_stop_confirmed"] = False
            else:
                self.metrics["caption_stop_confirmed"] = confirmed is True
        if self._start_inflight is not None:
            try:
                self._handle = await asyncio.wait_for(asyncio.shield(self._start_inflight), timeout=10)
            except TimeoutError:
                # The worker lease has expired. If SDK submission eventually
                # returns, cancel only that invocation; no extra polling task.
                def cancel_late(task):
                    if not task.cancelled() and task.exception() is None:
                        asyncio.create_task(asyncio.to_thread(self.bridge.stop, self.session_id, task.result()))
                self._start_inflight.add_done_callback(cancel_late)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Inference submission ended during shutdown (%s)", type(exc).__name__)
            self._start_inflight = None
        await self._stop_worker()
        await self.monitoring.close_sources()
        self.playing = False
        self._set_state("stopped")
        self.metrics["shutdown_total_seconds"] = time.perf_counter() - closing_started
        self._closed = True
