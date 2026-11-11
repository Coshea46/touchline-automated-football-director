"""Replay captions operations; ReplaySession retains ownership of shared state."""

import asyncio
import math
import re
from touchline.captions.live.events import MAX_CAPTION_TEXT_CHARS, MAX_CAPTION_WORD_CHARS, MAX_CAPTION_WORDS
from touchline.captions.limits import wall_allowance_seconds


class CaptionCoordinator:
    def __init__(self, session):
        self.session = session

    def caption_state(self):
        session = self.session
        return {"available": session.caption_worker is not None, "configured": session._caption_configured,
                "user_enabled": session._caption_user_enabled, "enabled": session._caption_enabled,
                "status": session._caption_status, "reason": session._caption_reason,
                "provider": "elevenlabs", "model": "scribe_v2_realtime",
                "generation": session._caption_generation, "match_id": session._caption_actual_match_id,
                "clock_epoch": session.clock_epoch, "deadline": session._caption_deadline,
                "max_audio_seconds": session.caption_max_audio_seconds,
                "max_cost_usd": session.caption_max_cost_usd}

    def configure_captions(self, worker, *, configured=None, reason=None):
        session = self.session
        if session._tasks:
            raise RuntimeError("Configure captions before starting the session")
        session.caption_worker = worker
        session._caption_configured = worker is not None if configured is None else bool(configured)
        session._caption_enabled = worker is not None and session._caption_user_enabled
        session._caption_status = "paused" if worker is not None else "unavailable"
        session._caption_reason = reason or ("awaiting_playback" if worker is not None else "missing_key")

    def caption_event(self, event):
        """The isolated SDK worker calls this from its own bounded reader thread."""
        session = self.session
        loop = session._caption_loop_owner
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(session._accept_caption_event, event)
            except RuntimeError:
                pass  # A final child callback may race event-loop shutdown.

    def _caption_revoke(self, reason, *, status="paused"):
        session = self.session
        if session.caption_worker is None:
            return
        incidental = {"viewer_disconnected", "manual_selection", "automatic_selection",
                      "source_preparing", "seek", "playback_inactive", "control_transferred"}
        if (not session._caption_enabled and session._caption_status in ("disabled", "unavailable", "stopped", "error")
                and reason in incidental):
            # A source transition or reconnect must not hide an exhausted
            # allowance/provider failure. Revocation still invalidates old data;
            # explicit On/Off and terminal updates remain authoritative.
            status, reason = session._caption_status, session._caption_reason
        session._caption_generation += 1
        session._caption_active_identity = None
        session._caption_status, session._caption_reason = status, reason
        if session.caption_worker is not None:
            try:
                session.caption_worker.stop(reason)
            except Exception:  # noqa: BLE001 - provider error text may contain credentials
                session._caption_enabled = False
                session._caption_status, session._caption_reason = "unavailable", "transport_error"

    def _caption_view(self):
        session = self.session
        if not session._caption_enabled or session.caption_worker is None or session._closing:
            return None
        if session._caption_deadline is not None and session.now() >= session._caption_deadline:
            return None
        if (session._controller_report_pending or session.last_report_at is None or session.monotonic() - session.last_report_at >= 5
                or session.controller is None or not session.playing or session.stalled or session._ended):
            return None
        match_id = session._caption_actual_match_id
        if match_id != session.director.current_match_id:
            return None
        if session._caption_pending_match_id is not None:
            if match_id == session._caption_pending_match_id:
                if session._caption_report_sequence == session._caption_preparing_report_sequence:
                    return None
                session._caption_pending_match_id = session._caption_pending_until = None
                session._caption_preparing_report_sequence = None
            elif session._caption_pending_until is None or session.now() <= session._caption_pending_until:
                return None
            else:
                session._caption_pending_match_id = session._caption_pending_until = None
        source = next((item for item in session.sources if item.match_id == match_id), None)
        if source is None:
            return None
        identity = source.metadata.get("source_identity", {}).get("digest") if source.metadata else None
        return {"session_id": session.session_id, "match_id": match_id, "source_key": source.key,
                "source_identity": identity, "source_url": source.source_url,
                "offset_seconds": source.offset_seconds, "position": session.position, "playing": True,
                "rate": session.rate, "stalled": False, "available": True, "active": True, "enabled": True,
                "clock_epoch": session.clock_epoch, "generation": session._caption_generation,
                "expires_at": session.now() + max(0, 5 - (session.monotonic() - session.last_report_at)),
                "report_received_at": session._caption_report_received_at, "deadline": session._caption_deadline}

    def _update_captions(self):
        session = self.session
        if session.caption_worker is None:
            return
        if session._caption_enabled and session._caption_deadline is not None and session.now() >= session._caption_deadline:
            session._caption_enabled = False
            session._caption_revoke("budget_deadline", status="stopped")
            session._publish_session()
            return
        source = next((item for item in session.sources if item.match_id == session._caption_actual_match_id), None)
        identity = source.metadata.get("source_identity", {}).get("digest") if source and source.metadata else None
        if session._caption_enabled and source is not None and (not isinstance(identity, str)
                or re.fullmatch(r"[a-f0-9]{64}", identity) is None):
            session._caption_enabled = False
            session._caption_revoke("source_identity_unverified", status="unavailable")
            session._publish_session()
            return
        view = session._caption_view()
        if view is None:
            if session._caption_active_identity is not None:
                session._caption_revoke("playback_inactive")
                session._publish_session()
            return
        if session._caption_deadline is None:
            # Loading, paused playback and the ML startup buffer consume no
            # caption allowance. The first actual ready focused-player report
            # starts one immutable bound shared by all toggles and generations.
            session._caption_deadline = session.now() + wall_allowance_seconds(
                session.caption_max_audio_seconds, session.caption_max_cost_usd)
            view["deadline"] = session._caption_deadline
        identity = (view["match_id"], view["clock_epoch"])
        if identity != session._caption_active_identity:
            session._caption_generation += 1
            view["generation"] = session._caption_generation
            session._caption_active_identity = identity
            session._caption_status, session._caption_reason = "connecting", None
            session._publish_session()
        try:
            session.caption_worker.update(view)
        except Exception:  # noqa: BLE001
            session._caption_enabled = False
            session._caption_revoke("transport_error", status="unavailable")
            session._publish_session()

    def _accept_caption_event(self, event):
        session = self.session
        def reject(reason):
            session.metrics['caption_events_rejected'] = session.metrics.get('caption_events_rejected', 0) + 1
            counts = session.metrics.setdefault('caption_rejection_counts', {})
            counts[reason] = counts.get(reason, 0) + 1
            session.metrics['caption_last_rejection_reason'] = reason
            return False

        if (not isinstance(event, dict) or not session._caption_enabled or session._closing
                or session._caption_active_identity is None or session._caption_view() is None
                or event.get("session_id") != session.session_id
                or type(event.get("generation")) is not int or type(event.get("clock_epoch")) is not int
                or event.get("generation") != session._caption_generation
                or event.get("clock_epoch") != session.clock_epoch
                or event.get("match_id") != session._caption_actual_match_id
                or event.get("model") != "scribe_v2_realtime"):
            return reject('inactive_or_identity')
        state = event.get("state")
        if state not in ("connecting", "listening", "partial", "committed", "paused", "unavailable", "stopped", "error"):
            return reject('invalid_state')
        text = event.get("text", "")
        if not isinstance(text, str) or len(text) > MAX_CAPTION_TEXT_CHARS:
            return reject('invalid_text_size')
        view = session._caption_view()
        if event.get("source_key") != view["source_key"] or event.get("source_identity") != view["source_identity"]:
            return reject('source_identity')
        result = {"type": "caption", "protocol_version": 1, "session_id": session.session_id,
                  "sequence": session._next_sequence(), "generation": session._caption_generation,
                  "clock_epoch": session.clock_epoch, "match_id": event["match_id"],
                  "source_key": view["source_key"], "source_identity": view["source_identity"],
                  "provider": "elevenlabs", "model": "scribe_v2_realtime", "state": state,
                  "text": text, "received_at": session.now()}
        anchor = event.get("source_anchor_seconds")
        valid_anchor = (isinstance(anchor, (int, float)) and not isinstance(anchor, bool)
                        and math.isfinite(anchor) and 0 <= anchor <= view["position"] + view["offset_seconds"] + 0.05)
        if text and not valid_anchor:
            return reject('invalid_anchor')
        if valid_anchor:
            result["source_anchor_seconds"] = anchor
        words = event.get("words")
        if words is not None:
            if not isinstance(words, list) or len(words) > MAX_CAPTION_WORDS:
                return reject('invalid_words_size')
            projected = []
            for word in words:
                if (not isinstance(word, dict) or not isinstance(word.get("text"), str)
                        or len(word["text"]) > MAX_CAPTION_WORD_CHARS):
                    return reject('invalid_word')
                start, end = word.get("start_seconds"), word.get("end_seconds")
                if (any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                        for value in (start, end)) or not valid_anchor
                        or not anchor <= start <= end <= view["position"] + view["offset_seconds"] + 0.05):
                    return reject('invalid_word_time')
                projected.append({"text": word["text"], "start_seconds": start, "end_seconds": end})
            result["words"] = projected
            if projected:
                result["playback_lag_seconds"] = max(0, view["position"] + view["offset_seconds"] - projected[-1]["end_seconds"])
        latency = event.get("latency_seconds")
        if (not isinstance(latency, bool) and isinstance(latency, (int, float))
                and math.isfinite(latency) and 0 <= latency <= 60):
            result["latency_seconds"] = latency
        reason = event.get("reason")
        if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,64}", reason):
            result["reason"] = reason
        if state in ("paused", "unavailable", "stopped", "error"):
            session._caption_reason = result.get("reason")
        session._caption_status = state
        session.metrics['caption_events_accepted'] = session.metrics.get('caption_events_accepted', 0) + 1
        session.publish(result)
        if state in ("unavailable", "stopped", "error"):
            session._caption_enabled = False
            session._caption_revoke(result.get("reason", "transport_error"), status=state)
            session._publish_session()
        return True

    async def _caption_loop(self):
        session = self.session
        while not session._closing:
            session._update_captions()
            await asyncio.sleep(0.1)

