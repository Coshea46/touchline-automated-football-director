"""Validate compact scores and select matches without fabricating probabilities."""

import math
from collections import deque
from dataclasses import asdict, dataclass, replace

from touchline.replay.sources import number


def positive_sequence(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class Score:
    match_id: str
    value: float
    sequence: int
    observation_start: float
    observation_end: float
    produced_at: float
    latency_seconds: float


@dataclass(frozen=True)
class DirectorSettings:
    cooldown_seconds: float
    staying_bonus: float
    confirmations_required: int
    emergency_enabled: bool = False
    emergency_margin: float = 0.50

    def __post_init__(self):
        number(self.cooldown_seconds, "cooldown_seconds", maximum=120)
        number(self.staying_bonus, "staying_bonus", maximum=1)
        if type(self.confirmations_required) is not int or not 1 <= self.confirmations_required <= 5:
            raise ValueError("confirmations_required must be an integer between 1 and 5")
        if type(self.emergency_enabled) is not bool:
            raise TypeError("emergency_enabled must be boolean")
        number(self.emergency_margin, "emergency_margin", minimum=0.05, maximum=1)


DIRECTOR_MODES = {
    "relaxed": DirectorSettings(20, 0.25, 3),
    "balanced": DirectorSettings(12, 0.20, 2),
    "all_action": DirectorSettings(4, 0.05, 1),
}


class Director:
    """At most two completed windows per match; no pending prediction queue.

    Compare a complete cohort at one common observation endpoint. This prevents
    whichever stream finishes first from winning against absent competing scores.
    """

    def __init__(self, match_ids, *, session_id, model_version, mode,
                 current_match_id=None, staying_bonus=None, minimum_switch_seconds=None,
                 confirmations_required=None, director_mode="balanced", emergency_enabled=False,
                 emergency_margin=0.50, maximum_score_age=3, maximum_playback_lag=3):
        self.match_ids = tuple(sorted(match_ids))
        if not self.match_ids or len(set(self.match_ids)) != len(self.match_ids):
            raise ValueError("Director requires unique registered matches")
        self.session_id, self.model_version, self.mode = session_id, model_version, mode
        self.current_match_id = current_match_id or self.match_ids[0]
        if self.current_match_id not in self.match_ids:
            raise ValueError("Unknown initial current match")
        if not isinstance(director_mode, str) or director_mode not in DIRECTOR_MODES:
            raise ValueError("Unknown director mode")
        self.director_mode = director_mode
        settings = DIRECTOR_MODES[director_mode]
        self.settings = DirectorSettings(
            settings.cooldown_seconds if minimum_switch_seconds is None else minimum_switch_seconds,
            settings.staying_bonus if staying_bonus is None else staying_bonus,
            settings.confirmations_required if confirmations_required is None else confirmations_required,
            emergency_enabled, emergency_margin)
        self.maximum_score_age = number(maximum_score_age, "maximum_score_age", minimum=0.1)
        self.maximum_playback_lag = number(maximum_playback_lag, "maximum_playback_lag", minimum=0.1)
        self.auto_enabled = True
        self.manual_hold_until = 0.0
        self.last_switch_at = float("-inf")
        self.last_automatic_switch_at = float("-inf")
        self.selection_kind, self.selection_reason = "initial", "initial_selection"
        self.history = deque(maxlen=100)
        self._history_sequence = 0
        self.healthy = dict.fromkeys(self.match_ids, True)
        self.rejected_records = 0
        self.reset(0)

    def reset(self, clock_epoch):
        self.clock_epoch = clock_epoch
        self.records = {match_id: deque(maxlen=2) for match_id in self.match_ids}
        self.last_worker_sequence = -1
        self.last_recommended_end = float("-inf")
        self._last_evaluation_end = float("-inf")
        self.latest_scores = {}
        self.scores_observation_end = self.scores_produced_at = self.scores_valid_until = None
        self.decision_reason = "waiting_for_scores"
        self.reset_confirmation()

    @property
    def staying_bonus(self):
        return self.settings.staying_bonus

    @property
    def minimum_switch_seconds(self):
        return self.settings.cooldown_seconds

    def reset_confirmation(self):
        self.challenger_id, self.challenger_wins = None, 0
        self._confirmation_end = None
        self._proposal = None

    def set_mode(self, mode):
        if not isinstance(mode, str) or mode not in DIRECTOR_MODES:
            raise ValueError("Unknown director mode")
        if mode == self.director_mode:
            return False
        self.settings = replace(DIRECTOR_MODES[mode], emergency_enabled=self.settings.emergency_enabled,
                                emergency_margin=self.settings.emergency_margin)
        self.director_mode = mode
        self.reset_confirmation()
        self.decision_reason = "mode_changed"
        return True

    def set_auto(self, enabled):
        if type(enabled) is not bool:
            raise TypeError("Auto setting must be boolean")
        if self.auto_enabled != enabled:
            self.auto_enabled = enabled
            self.reset_confirmation()

    def state(self, *, now, playback_position):
        fresh = (self.scores_valid_until is not None and now <= self.scores_valid_until
                 and self.scores_observation_end <= playback_position + 0.05
                 and playback_position - self.scores_observation_end <= self.maximum_playback_lag)
        return {"mode": self.director_mode, "config": asdict(self.settings),
                "selection_kind": self.selection_kind, "selection_reason": self.selection_reason,
                "decision_reason": self.decision_reason,
                "last_switch_at": self.last_switch_at if math.isfinite(self.last_switch_at) else None,
                "auto_cooldown_until": (self.last_switch_at + self.minimum_switch_seconds
                                        if math.isfinite(self.last_switch_at) else None),
                "challenger_id": self.challenger_id, "challenger_wins": self.challenger_wins,
                "latest_scores": dict(self.latest_scores), "scores_observation_end": self.scores_observation_end,
                "scores_produced_at": self.scores_produced_at, "scores_valid_until": self.scores_valid_until,
                "scores_fresh": bool(fresh), "prediction_stride_seconds": 2,
                "history": [dict(event) for event in self.history]}

    def manual_select(self, match_id, *, now, playback_position=None):
        if match_id not in self.records:
            raise ValueError("Unknown match ID")
        self.reset_confirmation()
        self.commit_selection(match_id, now=now, kind="manual", reason="manual_selection",
                              playback_position=playback_position)
        self.selection_kind, self.selection_reason = "manual", "manual_selection"
        self.manual_hold_until = now + 30

    def commit_selection(self, match_id, *, now, kind="playback", reason=None, playback_position=None, score=None):
        if match_id not in self.records:
            raise ValueError("Unknown match ID")
        if kind not in ("automatic", "manual", "playback", "fallback"):
            raise ValueError("Unknown selection kind")
        previous = self.current_match_id
        if match_id == previous:
            return False
        self.selection_kind, self.selection_reason = kind, reason or "playback_feedback"
        self.current_match_id, self.last_switch_at = match_id, now
        if kind in ("automatic", "fallback"):
            self.last_automatic_switch_at = now
        if score is None and self.scores_valid_until is not None and now <= self.scores_valid_until:
            score = self.latest_scores.get(match_id)
        self._history_sequence += 1
        self.history.append({"id": self._history_sequence, "from_match_id": previous, "to_match_id": match_id,
                             "selected_at": now, "playback_time": playback_position, "kind": kind,
                             "score": score, "reason": self.selection_reason})
        self.reset_confirmation()
        return True

    def _valid(self, record, *, now, playback_position, snapshot_epoch):
        if not isinstance(record, dict):
            return None
        match_id = record.get("match_id")
        if (not isinstance(match_id, str) or match_id not in self.records or record.get("session_id") != self.session_id
                or record.get("model_version") != self.model_version
                or record.get("clock_epoch", snapshot_epoch) != self.clock_epoch
                or record.get("status", "healthy") not in ("healthy", "running", "ready")):
            return None
        sequence = record.get("sequence")
        if not positive_sequence(sequence):
            return None
        try:
            value = number(record.get("score"), "score", maximum=1)
            start = number(record.get("observation_start"), "observation_start")
            end = number(record.get("observation_end"), "observation_end")
            produced = number(record.get("produced_at"), "produced_at")
            latency = number(record.get("latency_seconds"), "latency_seconds")
        except ValueError:
            return None
        if (abs(end - start - 5) > 1e-5 or end > playback_position + 0.05
                or playback_position - end > self.maximum_playback_lag
                or produced > now + 1 or now - produced > self.maximum_score_age
                or latency > self.maximum_score_age):
            return None
        previous = self.records[match_id]
        if previous and (sequence <= previous[-1].sequence or end <= previous[-1].observation_end):
            return None
        return Score(match_id, value, sequence, start, end, produced, latency)

    def ingest(self, snapshot, *, now, playback_position):
        if (not isinstance(snapshot, dict) or snapshot.get("session_id") != self.session_id
                or snapshot.get("model_version") != self.model_version
                or snapshot.get("clock_epoch") != self.clock_epoch
                or snapshot.get("score_kind") != ("model" if self.mode == "real" else "mock")):
            return False
        sequence = snapshot.get("worker_sequence")
        if not positive_sequence(sequence) or sequence <= self.last_worker_sequence:
            return False
        self.last_worker_sequence = sequence
        health = snapshot.get("health", {})
        if isinstance(health, dict):
            for match_id in self.match_ids:
                value = health.get(match_id)
                if isinstance(value, dict):
                    value = value.get("status")
                if value in ("healthy", "running", "ready"):
                    self.healthy[match_id] = True
                elif value in ("error", "failed", "unavailable", "eof", "stopped"):
                    self.healthy[match_id] = False
        records = snapshot.get("scores")
        if not isinstance(records, list) or len(records) > len(self.match_ids):
            return False
        changed = False
        seen = set()
        for record in records:
            match_id = record.get("match_id") if isinstance(record, dict) else None
            if not isinstance(match_id, str) or match_id in seen:
                self.rejected_records += 1
                continue
            seen.add(match_id)
            valid = self._valid(record, now=now, playback_position=playback_position,
                                snapshot_epoch=snapshot.get("clock_epoch"))
            if valid is None:
                self.rejected_records += 1
                continue
            self.records[valid.match_id].append(valid)
            changed = True
        return changed

    def decide(self, *, now, playback_position, commit=True):
        healthy_ids = [match_id for match_id in self.match_ids if self.healthy[match_id]]
        if not healthy_ids:
            self.decision_reason = "waiting_for_scores"
            return None
        candidates = {}
        for match_id in healthy_ids:
            for record in self.records[match_id]:
                if (record.observation_end <= playback_position + 0.05
                        and playback_position - record.observation_end <= self.maximum_playback_lag
                        and -1 <= now - record.produced_at <= self.maximum_score_age):
                    # The worker uses one shared replay grid. Rounding absorbs JSON float noise.
                    candidates.setdefault(round(record.observation_end, 6), {})[match_id] = record
        endpoints = [end for end, cohort in candidates.items()
                     if len(cohort) == len(healthy_ids) and end >= self._last_evaluation_end]
        if not endpoints:
            self.decision_reason = "waiting_for_scores"
            return None
        end = max(endpoints)
        cohort = candidates[end]
        new_evaluation = end > self._last_evaluation_end
        current = self.current_match_id
        winner = min(healthy_ids, key=lambda match_id: (-cohort[match_id].value, match_id))
        current_healthy = current in cohort
        gap = cohort[winner].value - (cohort[current].value + self.staying_bonus) if current_healthy else None
        if current_healthy and (winner == current or gap <= 1e-12):
            winner = current  # A healthy current match wins adjusted-score ties.
        if new_evaluation:
            if winner == current or not current_healthy:
                self.reset_confirmation()
            else:
                consecutive = (self.challenger_id == winner and self._confirmation_end is not None
                               and abs(end - self._confirmation_end - 2) < 1e-5)
                self.challenger_wins = min(self.settings.confirmations_required,
                                           self.challenger_wins + 1 if consecutive else 1)
                self.challenger_id, self._confirmation_end = winner, end
            self._last_evaluation_end = end
            self.latest_scores = {match_id: cohort[match_id].value for match_id in healthy_ids}
            self.scores_observation_end = end
            self.scores_produced_at = min(record.produced_at for record in cohort.values())
            self.scores_valid_until = min(record.produced_at + self.maximum_score_age for record in cohort.values())
        elif winner == current or (self.challenger_id is not None and winner != self.challenger_id):
            self.reset_confirmation()  # Health-only changes cannot confirm a challenger.
        selected, kind = current, "automatic"
        if not self.auto_enabled:
            reason = "auto_disabled"
        elif now < self.manual_hold_until:
            reason = "manual_hold"
        elif now < self.last_switch_at + self.minimum_switch_seconds:
            reason = "automatic_cooldown"
        elif not current_healthy:
            selected, kind, reason = winner, "fallback", "unavailable_fallback"
        elif winner == current:
            reason = "current_leading"
        elif self.settings.emergency_enabled and gap >= self.settings.emergency_margin:
            selected, reason = winner, "strong_challenger"
        elif self.challenger_id == winner and self.challenger_wins >= self.settings.confirmations_required:
            selected, reason = winner, "challenger_confirmed"
        else:
            reason = "confirming_challenger"
        self.decision_reason = reason
        signature = (end, selected, reason, self.director_mode)
        if not new_evaluation and (selected == current or signature == self._proposal):
            return None
        self._proposal = signature if selected != current else None
        if commit and selected != self.current_match_id:
            self.commit_selection(selected, now=now, kind=kind, reason=reason,
                                  playback_position=playback_position, score=cohort[selected].value)
        self.last_recommended_end = end
        if selected not in cohort:
            return None  # Auto off on an unavailable source: state updates, no invalid switch message.
        return {"selected_match_id": selected, "observation_end": end,
                "selection_reason": reason, "selection_kind": kind if selected != current else self.selection_kind,
                "scores": {match_id: cohort[match_id].value for match_id in healthy_ids},
                "valid_until": min(record.produced_at + self.maximum_score_age for record in cohort.values()),
                "worker_completion_to_backend_seconds": max(max(0, now - record.produced_at)
                                                              for record in cohort.values()),
                "latency_seconds": max(record.latency_seconds + max(0, now - record.produced_at)
                                       for record in cohort.values())}
