"""Replay monitoring operations; ReplaySession retains ownership of shared state."""

import logging
import asyncio
import math
import time
from touchline.ml_inference.streaming.contract import MATCHES_PER_GPU


from touchline.replay.diagnostics import worker_diagnostics, transient_transport_error

logger = logging.getLogger(__name__)

class MonitoringCoordinator:
    def __init__(self, session):
        self.session = session

    @property
    def auto_starting(self):
        session = self.session
        return bool(session._prepared_config is not None and not session._worker_ready
            and session.state not in ('error', 'ended', 'stopped')
            and (session._activation_requested or session._activation_inflight or session._start_authorized or session._start_inflight is not None
                 or session._handle is not None) and not session._closing)

    def auto_start_status(self):
        session = self.session
        if session._closing:
            return False, 'closing'
        if session._prepared_config is None:
            return False, session._preparation_error or 'not_prepared'
        if session._finished and session._finished_reason == 'worker_error':
            return False, 'model_unavailable'
        if session.auto_starting:
            return False, 'starting'
        if session._handle is not None or session._start_inflight is not None or session._stopping_handle is not None:
            return False, 'monitoring_active'
        if session._ended:
            return False, 'source_ended'
        elapsed = 0 if session._monitor_started_at is None else max(0, session.monotonic() - session._monitor_started_at)
        remaining = session._prepared_config.max_duration_seconds - elapsed
        if remaining < 5:
            return False, 'session_expired'
        workers = session._prepared_config.gpu_workers or math.ceil(len(session.registry.sources) / MATCHES_PER_GPU)
        from touchline.ml_inference.streaming.contract import MAX_WORKER_RATE
        if (remaining + 330) * workers * MAX_WORKER_RATE > session._prepared_config.max_estimated_cost_usd - session._reserved_compute_usd:
            return False, 'budget_exhausted'
        return True, 'available'

    async def _activate_prepared_monitoring(self):
        session = self.session
        session._activation_requested = False
        # Recheck after the socket handler queued its intent; disconnect/Off or
        # a service limit can overtake this background control tick.
        if not session.director.auto_enabled or session._closing:
            session._start_authorized = False
            return
        can_start, reason = session.auto_start_status()
        # The queued authorization itself makes status starting. Temporarily
        # remove it to evaluate immutable service time/budget constraints.
        if session._start_authorized:
            session._start_authorized = False
            can_start, reason = session.auto_start_status()
        if not can_start:
            session._set_state('stopped', 'auto_' + reason)
            return
        if session.bridge is None:
            session._activation_inflight = True
            try:
                if session._bridge_factory is None:
                    from touchline.ml_inference.streaming.ws_client import ReplayWebSocketClient
                    session.bridge = await asyncio.to_thread(ReplayWebSocketClient.from_env)
                else:
                    session.bridge = await asyncio.to_thread(session._bridge_factory)
            finally:
                session._activation_inflight = False
        if not session.director.auto_enabled or session._closing:
            session._start_authorized = False
            session._set_state('disabled' if not session.config.monitor else 'stopped')
            return
        session.config = session._prepared_config
        session.director.mode, session.director.model_version = session.config.mode, session.config.model_version
        session.director.reset(session.clock_epoch)
        session._last_recommendation = None
        session._finished, session._stop_requested = False, False
        session._start_authorized = True
        session._set_state('starting')

    @property
    def auto_available(self):
        """A requested Auto preference becomes active only after worker readiness."""
        session = self.session
        return bool(session.config.monitor and session.config.mode in ("real", "features_mock")
                    and session._worker_ready and session.state in ("running", "degraded", "paused")
                    and not (session._finished or session._stop_requested or session._closing))

    def control(self, *, active=True):
        session = self.session
        now = session.now()
        fresh = (not session._controller_report_pending and session.last_report_at is not None
                 and session.monotonic() - session.last_report_at < session.config.heartbeat_seconds)
        result = {"session_id": session.session_id, "clock_epoch": session.clock_epoch,
                  "position": session.position, "playing": session.playing and fresh,
                  "startup_waiting": not session._worker_ready,
                  "rate": session.rate, "active": bool(active and fresh and session.controller is not None),
                  "expires_at": (now + max(0, session.config.heartbeat_seconds
                                             - (session.monotonic() - session.last_report_at))
                                 if fresh and active else now),
                  "updated_at": now}
        # Include the latest credentials until a worker actually observes them;
        # a one-shot update can be overwritten while it is busy assembling.
        result["sources"] = [source.worker_spec() for source in session.sources]
        return result

    def worker_spec(self):
        session = self.session
        elapsed = (0 if session._monitor_started_at is None else
                   max(0, session.monotonic() - session._monitor_started_at))
        spec = {"session_id": session.session_id, "model_version": session.config.model_version,
                "mode": session.config.mode, "checkpoint": session.config.checkpoint,
                "clock_epoch": session.clock_epoch, "start_position": session.position,
                "matches": [source.worker_spec() for source in session.sources],
                "max_duration_seconds": max(5, session.config.max_duration_seconds - elapsed),
                "max_estimated_cost_usd": session.config.max_estimated_cost_usd - session._reserved_compute_usd}
        spec["gpu_workers"] = session.config.gpu_workers or math.ceil(len(session.sources) / MATCHES_PER_GPU)
        if session.config.mock_scores is not None:
            spec["mock_scores"] = dict(session.config.mock_scores)
        return spec

    async def _stop_worker(self):
        session = self.session
        handle, session._handle = session._handle or session._stopping_handle, None
        session._worker_ready = False
        if handle is None:
            if session._finished or session._stop_requested:
                session._start_authorized = session._activation_requested = False
            return
        session._start_authorized = session._activation_requested = False
        session._stopping_handle = handle
        session.metrics["worker_stop_succeeded"] = False
        started = time.perf_counter()
        try:
            await asyncio.wait_for(session._control_rpc(session.control(active=False)), session.config.shutdown_rpc_seconds)
            session.metrics["shutdown_control_status"] = "completed"
        except TimeoutError:
            session.metrics["shutdown_control_status"] = "timeout"
            session.metrics["shutdown_sdk_thread_may_remain"] = True
            logger.warning("Inference shutdown control timed out; last viewer lease still expires")
        except Exception as exc:  # noqa: BLE001 - lease remains independent safety net
            session.metrics["shutdown_control_status"] = type(exc).__name__
            logger.warning("Inference control stop failed (%s)", type(exc).__name__)
        try:
            result = await asyncio.wait_for(asyncio.to_thread(session.bridge.stop, session.session_id, handle),
                                            session.config.shutdown_rpc_seconds)
            confirmed = (result.get("confirmed") is True if isinstance(result, dict) else
                         getattr(session.bridge, "transport", None) != "websocket")
            session.metrics["worker_stop_succeeded"] = confirmed
            session.metrics["shutdown_cancel_status"] = "completed" if confirmed else "unconfirmed"
            if isinstance(result, dict) and result.get("method") in ("websocket_graceful", "modal_input_cancel"):
                session.metrics["owned_stop_method"] = result["method"]
        except TimeoutError:
            session.metrics["shutdown_cancel_status"] = "timeout"
            session.metrics["shutdown_sdk_thread_may_remain"] = True
            logger.warning("Inference input cancellation timed out; cleanup is unconfirmed")
        except Exception as exc:  # noqa: BLE001
            session.metrics["shutdown_cancel_status"] = type(exc).__name__
            logger.warning("Inference input cancellation failed (%s)", type(exc).__name__)
        session.metrics["shutdown_worker_seconds"] = time.perf_counter() - started
        session._stopping_handle = None

    async def _control_rpc(self, control):
        session = self.session
        started = time.perf_counter()
        try:
            return await asyncio.to_thread(session.bridge.update_control, session.session_id, control)
        finally:
            elapsed = time.perf_counter() - started
            session.metrics["control_rpc_count"] += 1
            session.metrics["control_rpc_seconds"] += elapsed
            session.metrics["control_rpc_last_seconds"] = elapsed

    async def _score_rpc(self):
        session = self.session
        started = time.perf_counter()
        try:
            snapshot = await asyncio.to_thread(session.bridge.latest, session.session_id)
            completed = session.now()
            session.metrics["score_read_completed_at"] = completed
            if isinstance(snapshot, dict):
                produced = snapshot.get("produced_at")
                if isinstance(produced, (int, float)) and math.isfinite(produced):
                    session.metrics["score_age_at_read_seconds"] = max(0, completed - produced)
                ages = [max(0, completed - record["produced_at"]) for record in snapshot.get("scores", [])
                        if isinstance(record, dict) and isinstance(record.get("produced_at"), (int, float))
                        and math.isfinite(record["produced_at"])]
                if ages:
                    session.metrics["score_record_age_at_read_seconds"] = max(ages)
            return snapshot
        finally:
            elapsed = time.perf_counter() - started
            session.metrics["score_rpc_count"] += 1
            session.metrics["score_rpc_seconds"] += elapsed
            session.metrics["score_rpc_last_seconds"] = elapsed

    async def _start_worker(self):
        session = self.session
        from touchline.ml_inference.streaming.contract import MAX_WORKER_RATE
        spec = session.worker_spec()
        # Reserve cold startup and idle overhead again for each attempted input,
        # so recovery cannot silently multiply the configured session allowance.
        estimate = (spec["max_duration_seconds"] + 330) * MAX_WORKER_RATE * spec["gpu_workers"]
        if estimate > spec["max_estimated_cost_usd"]:
            session._start_authorized = False
            session._finished = True
            session._finished_reason = "session_budget_reached"
            session._set_state("stopped", session._finished_reason)
            return
        if session._prepared_config is None:
            session._reserved_compute_usd += estimate
            session.metrics["reserved_compute_usd"] = session._reserved_compute_usd
        session._set_state("starting")
        await session._control_rpc(session.control())
        if session._prepared_config is not None:
            if not session._start_authorized or not session.director.auto_enabled:
                session._set_state('disabled')
                return
            if not session.control()['active']:
                session._set_state('waiting')
                return
            spec = session.worker_spec()
            estimate = (spec['max_duration_seconds'] + 330) * MAX_WORKER_RATE * spec['gpu_workers']
            if estimate > spec['max_estimated_cost_usd']:
                session._start_authorized = False
                session._set_state('stopped', 'session_budget_reached')
                return
            session._reserved_compute_usd += estimate
            session.metrics['reserved_compute_usd'] = session._reserved_compute_usd
        session._activation_requested = False
        session._start_authorized = False
        # Shield the submitted RPC: cancellation cannot stop a running thread.
        # Retaining its task lets shutdown recover and cancel the owned handle.
        session._start_inflight = asyncio.create_task(asyncio.to_thread(session.bridge.start, spec))
        try:
            session._handle = await asyncio.shield(session._start_inflight)
        except BaseException:
            if session._start_inflight.done():
                session._start_inflight = None
            raise
        session.metrics["worker_stop_succeeded"] = False
        session._start_inflight = None
        session._sources_changed = False
        if session._monitor_started_at is None:
            session._monitor_started_at = session.monotonic()

    async def _control_tick(self):
        session = self.session
        now = session.monotonic()
        if session._activation_requested and not session._controller_report_pending:
            await session._activate_prepared_monitoring()
        if session.registry.needs_refresh():
            try:
                session.sources = await asyncio.to_thread(session.registry.resolve)
                session._sources_changed = True
                session._publish_session()
            except Exception as exc:  # noqa: BLE001 - URL/credential errors stay private
                logger.warning("Media authorisation refresh failed (%s)", type(exc).__name__)
                session._set_state("degraded", "source_refresh_failed")
        if not session.config.monitor:
            return
        if (session._monitor_started_at is not None
                and now - session._monitor_started_at >= session.config.max_duration_seconds):
            session._finished = True
        fresh = session.last_report_at is not None and now - session.last_report_at < session.config.heartbeat_seconds
        if (session._controller_report_pending and fresh
                and not (session._stop_requested or session._finished or session._ended)):
            # The previous report remains the worker's only permit until its
            # original expiry. A handoff itself never creates another GPU input.
            return
        paused_idle = (session._worker_ready and session._ready_at is not None
                       and now - session._ready_at > session.config.pause_idle_seconds
                       and session._paused_at is not None
                       and now - session._paused_at >= session.config.pause_idle_seconds)
        stop = session._stop_requested or session._finished or session._ended or not fresh or paused_idle
        if stop:
            had_worker = session._handle is not None
            await session._stop_worker()
            session._stop_requested = False
            if had_worker and not session._restart_after_seek:
                session.clock_epoch += 1
                session.director.reset(session.clock_epoch)
            session._restart_after_seek = False
            if session._finished:
                session._set_state("stopped", session._finished_reason)
            elif session._ended:
                session._set_state("ended")
            elif paused_idle:
                session._set_state("paused")
            elif not fresh:
                session._set_state("waiting")
            if not fresh or session._finished or session._ended or paused_idle:
                return
        if session._handle is None:
            if session._prepared_config is not None and not session._start_authorized:
                return
            if (session._monitor_started_at is not None
                    and session.config.max_duration_seconds - (now - session._monitor_started_at) < 5):
                session._finished = True
                session._set_state("stopped", "session_limit_reached")
                return
            if session.state == "paused" and not session.playing:
                return
            explicit_start = session._prepared_config is not None and session._start_authorized
            if (session._restarts > session.config.max_restarts and not explicit_start) or now < session._retry_at:
                return
            await session._start_worker()
        else:
            await session._control_rpc(session.control())
            session._sources_changed = False
            if session._worker_ready and session._ready_at is not None and now - session._ready_at > 1:
                state = "running" if session.playing else "paused"
                if session.playing and not all(session.director.healthy.values()):
                    state = "degraded"
                session._set_state(state)

    async def _control_loop(self):
        session = self.session
        while not session._closing:
            tick_started = session.monotonic()
            try:
                await session._control_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - bounded recovery, secrets excluded
                session.metrics["worker_failures"] += 1
                session._restarts += 1
                session._retry_at = session.monotonic() + 5
                await session._stop_worker()
                session.clock_epoch += 1
                session.director.reset(session.clock_epoch)
                session._set_state("error", "inference_unavailable")
                logger.warning("Inference lifecycle failed (%s), recovery attempt %d",
                               type(exc).__name__, session._restarts)
            await asyncio.sleep(max(0, session.config.control_seconds - (session.monotonic() - tick_started)))

    def accept_snapshot(self, snapshot):
        """Also used by bounded real bridge-to-WebSocket validation."""
        session = self.session
        if not isinstance(snapshot, dict):
            return False
        if session._finished or session._stop_requested or session._closing:
            return False
        identity = (snapshot.get("session_id") == session.session_id
                    and snapshot.get("model_version") == session.config.model_version
                    and snapshot.get("clock_epoch") == session.clock_epoch)
        if not identity:
            return False
        if (snapshot.get("score_kind") != ("model" if session.config.mode == "real" else "mock")
                or not isinstance(snapshot.get("worker_sequence"), int)
                or isinstance(snapshot.get("worker_sequence"), bool)
                or snapshot["worker_sequence"] <= session.director.last_worker_sequence):
            return False
        diagnostics = worker_diagnostics(snapshot)
        session.metrics["worker_last_state"] = diagnostics.pop("worker_state")
        session.metrics.update(diagnostics)
        health = snapshot.get("health", {})
        if isinstance(health, dict):
            session.worker_health = {match_id: {field: value.get(field) for field in
                ("status", "next_sample_index", "queue_samples")} for match_id, value in health.items()
                if match_id in session.director.records and isinstance(value, dict)}
            for match_id in session.director.match_ids:
                status = health.get(match_id)
                if isinstance(status, dict):
                    status = status.get("status")
                if status in ("healthy", "running", "ready"):
                    session._model_health[match_id] = True
                elif status in ("error", "failed", "unavailable", "eof", "stopped"):
                    session._model_health[match_id] = False
        if snapshot.get("state") == "error":
            session.director.last_worker_sequence = snapshot["worker_sequence"]
            session._last_recommendation = None
            session.director.reset_confirmation()
            session._stop_requested = True
            if transient_transport_error(snapshot):
                session._restarts += 1
                session._retry_at = session.monotonic() + 5
                session.metrics["worker_failures"] += 1
                session.metrics["transport_recoveries"] = session._restarts
                session._set_state("error", "inference_unavailable")
            else:
                session._finished = True
                session._finished_reason = "worker_error"
                session._set_state("error", "worker_error")
            return False
        if snapshot.get("state") == "stopped":
            session.director.last_worker_sequence = snapshot["worker_sequence"]
            session._last_recommendation = None
            session.director.reset_confirmation()
            reason = snapshot.get("reason")
            session._stop_requested = True
            if reason in ("session_limit", "all_sources_ended"):
                session._finished = True
                session._finished_reason = "all_sources_ended" if reason == "all_sources_ended" else "session_limit_reached"
                session._set_state("ended" if reason == "all_sources_ended" else "stopped", session._finished_reason)
            else:
                session._restarts += 1
                session._retry_at = session.monotonic() + 5
                session._set_state("error", "worker_lease_expired")
            return False
        if snapshot.get("state") in ("ready", "running") and not session._worker_ready:
            session._worker_ready = True
            session._ready_at = session.monotonic()
            session._set_state("running")
        started = time.perf_counter()
        previous_health = session.director.healthy.copy()
        changed = session.director.ingest(snapshot, now=session.now(), playback_position=session.position)
        for match_id in session.director.match_ids:
            session.director.healthy[match_id] = session._model_health[match_id] and match_id not in session._unplayable
        if previous_health != session.director.healthy:
            if (session._last_recommendation is not None
                    and not session.director.healthy.get(session._last_recommendation["selected_match_id"], False)):
                session._last_recommendation = None
            if (session.director.challenger_id is not None
                    and not session.director.healthy.get(session.director.challenger_id, False)):
                session.director.reset_confirmation()
            session._publish_session()
        previous_end = session.director.scores_observation_end
        emitted = session._evaluate_director() if changed or previous_health != session.director.healthy else False
        session.metrics["director_seconds"] = time.perf_counter() - started
        if not emitted and previous_end != session.director.scores_observation_end:
            session._publish_session()
        return emitted

    def _evaluate_director(self):
        session = self.session
        if (session._finished or session._stop_requested or session._closing or session._controller_report_pending
                or not session.auto_available):
            return False
        decision = session.director.decide(now=session.now(), playback_position=session.position, commit=False)
        if decision is None:
            return False
        session.metrics["recommendations"] += 1
        session._last_recommendation = {"type": "recommendation", "protocol_version": 1,
                                     "session_id": session.session_id, "sequence": session._next_sequence(),
                                     "model_version": session.config.model_version, "clock_epoch": session.clock_epoch,
                                     "score_kind": "model" if session.config.mode == "real" else "mock",
                                     "playback_time": session.position, "emitted_at": session.now(), **decision,
                                     "director": session.director.state(now=session.now(), playback_position=session.position)}
        if decision["selected_match_id"] != session.director.current_match_id:
            session._caption_pending_match_id = decision["selected_match_id"]
            session._caption_pending_until = decision.get("valid_until", session.now())
            session._caption_revoke("automatic_selection")
            session._publish_session()
        session.publish(session._last_recommendation)
        return True

    async def _score_loop(self):
        session = self.session
        while not session._closing:
            tick_started = session.monotonic()
            if session._handle is not None:
                try:
                    snapshot = await session._score_rpc()
                    session.accept_snapshot(snapshot)
                    # A cooldown can expire between inference ticks. Reuse only
                    # the still-fresh cohort; this never adds a confirmation.
                    session._evaluate_director()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    session._stop_requested = True
                    session._restarts += 1
                    session._retry_at = session.monotonic() + 5
                    session.metrics["worker_failures"] += 1
                    session._set_state("error", "score_transport_failed")
                    logger.warning("Inference score read failed (%s)", type(exc).__name__)
            await asyncio.sleep(max(0, session.config.poll_seconds - (session.monotonic() - tick_started)))

