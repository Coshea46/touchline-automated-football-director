"""Offline tests for unit replay test_director_policy."""


import math
import pytest
from touchline.replay.config import SessionConfig
from tests.support.replay.session import scores
from tests.support.replay.director import stable, evaluate


@pytest.mark.parametrize("mode,cooldown,bonus,confirmations", [
    ("balanced", 12, 0.20, 2), ("relaxed", 20, 0.25, 3), ("all_action", 4, 0.05, 1),
])
def test_modes_have_server_owned_exact_settings(mode, cooldown, bonus, confirmations):
    d = stable(director_mode=mode)
    settings = d.state(now=1000, playback_position=0)["config"]
    assert settings == {"cooldown_seconds": cooldown, "staying_bonus": bonus,
                        "confirmations_required": confirmations, "emergency_enabled": False, "emergency_margin": 0.5}
    if mode == "balanced":
        assert stable().settings == d.settings
        assert SessionConfig.from_env({}).director_mode == mode


@pytest.mark.parametrize("env", [
    {"MATCHPULSE_DIRECTOR_MODE": "unknown"},
    {"MATCHPULSE_DIRECTOR_EMERGENCY_ENABLED": "yes"},
    {"MATCHPULSE_DIRECTOR_EMERGENCY_MARGIN": "nan"},
])
def test_invalid_operator_settings_are_rejected_before_monitoring(env):
    with pytest.raises(ValueError):
        SessionConfig.from_env(env)


def test_balanced_requires_same_challenger_two_fresh_endpoints_and_does_not_commit_proposal():
    d = stable()
    first = evaluate(d, 5, 1000)
    assert first["selected_match_id"] == "cam-1" and d.challenger_wins == 1
    assert d.decision_reason == "confirming_challenger"
    assert not d.ingest(scores(), now=1000, playback_position=5)
    assert d.decide(now=1000.1, playback_position=5) is None
    assert d.challenger_wins == 1
    second = evaluate(d, 7, 1002)
    assert second["selected_match_id"] == "cam-2" and d.challenger_wins == 2
    assert d.current_match_id == "cam-1" and not math.isfinite(d.last_automatic_switch_at)
    assert d.decide(now=1002.1, playback_position=7) is None
    assert d.latest_scores == {"cam-1": 0.4, "cam-2": 0.8}


def test_health_only_updates_and_new_sequences_for_old_windows_cannot_confirm_again():
    d = stable()
    evaluate(d, 5, 1000)
    repeated = scores(sequence=2)
    repeated["scores"] = []
    assert not d.ingest(repeated, now=1000.5, playback_position=5)
    assert d.decide(now=1000.5, playback_position=5) is None
    assert d.challenger_wins == 1
    assert not d.ingest(scores(sequence=3), now=1000.5, playback_position=5)
    assert d.decide(now=1000.5, playback_position=5) is None
    assert d.challenger_wins == 1


def test_challenger_change_current_recovery_and_missing_endpoint_restart_confirmation():
    d = stable(ids=("cam-1", "cam-2", "cam-3"))
    evaluate(d, 5, 1000, {"cam-1": 0.1, "cam-2": 0.8, "cam-3": 0.4})
    result = evaluate(d, 7, 1002, {"cam-1": 0.1, "cam-2": 0.4, "cam-3": 0.9})
    assert result["selected_match_id"] == "cam-1" and d.challenger_id == "cam-3" and d.challenger_wins == 1
    assert evaluate(d, 9, 1004, {"cam-1": 0.1, "cam-2": 0.4, "cam-3": 0.9})["selected_match_id"] == "cam-3"
    assert evaluate(d, 11, 1006, {"cam-1": 0.8, "cam-2": 0.4, "cam-3": 0.9})["selected_match_id"] == "cam-1"
    assert d.challenger_wins == 0
    evaluate(d, 13, 1008, {"cam-1": 0.1, "cam-2": 0.9, "cam-3": 0.4})
    assert evaluate(d, 17, 1012, {"cam-1": 0.1, "cam-2": 0.9, "cam-3": 0.4})["selected_match_id"] == "cam-1"
    assert d.challenger_wins == 1


def test_adjusted_tie_retains_current_even_when_other_id_sorts_first():
    d = stable(current_match_id="cam-2")
    for end, now in ((5, 1000), (7, 1002), (9, 1004)):
        assert evaluate(d, end, now, {"cam-1": 0.5, "cam-2": 0.3})["selected_match_id"] == "cam-2"
    assert d.challenger_wins == 0
    d = stable(ids=("cam-1", "cam-2", "cam-3"), current_match_id="cam-2")
    evaluate(d, 5, 1000, {"cam-1": 0.9, "cam-2": 0.1, "cam-3": 0.9})
    assert evaluate(d, 7, 1002, {"cam-1": 0.9, "cam-2": 0.1, "cam-3": 0.9})["selected_match_id"] == "cam-1"


def test_scores_continue_every_two_seconds_during_twelve_second_cooldown_then_latest_can_be_used():
    d = stable()
    d.commit_selection("cam-2", now=1000, kind="automatic", reason="challenger_confirmed")
    decisions = [evaluate(d, end, 1001 + index * 2, {"cam-1": 0.9, "cam-2": 0.1})
                 for index, end in enumerate((5, 7, 9, 11, 13, 15))]
    assert all(result["selected_match_id"] == "cam-2" for result in decisions)
    assert all(result["selection_reason"] == "automatic_cooldown" for result in decisions)
    assert d.scores_observation_end == 15 and d.challenger_wins == 2
    proposal = d.decide(now=1012, playback_position=15, commit=False)
    assert proposal["selected_match_id"] == "cam-1" and d.challenger_wins == 2
    assert d.last_automatic_switch_at == 1000  # Proposal is not acknowledgement.
    assert d.decide(now=1012.1, playback_position=15) is None


def test_expired_latest_window_never_switches_when_cooldown_ends():
    d = stable()
    d.commit_selection("cam-2", now=1000, kind="automatic")
    evaluate(d, 5, 1001, {"cam-1": 0.9, "cam-2": 0.1})
    evaluate(d, 7, 1003, {"cam-1": 0.9, "cam-2": 0.1})
    assert d.decide(now=1012, playback_position=7) is None
    assert d.current_match_id == "cam-2"


def test_manual_epoch_and_mode_changes_clear_confirmation_without_reusing_the_old_window():
    d = stable()
    evaluate(d, 5, 1000)
    d.manual_select("cam-1", now=1000.5, playback_position=5)
    assert d.challenger_wins == 0 and d.manual_hold_until == 1030.5
    assert d.decide(now=1001, playback_position=5) is None
    assert evaluate(d, 7, 1002)["selection_reason"] == "manual_hold"
    d.reset(1)
    assert d.challenger_wins == 0 and d.latest_scores == {}
    evaluate(d, 5, 1004)
    d.set_mode("relaxed")
    assert d.challenger_wins == 0 and d.minimum_switch_seconds == 20
    assert d.decide(now=1004.1, playback_position=5) is None
    for end, now in ((7, 1031), (9, 1033)):
        assert evaluate(d, end, now)["selected_match_id"] == "cam-1"
    assert evaluate(d, 11, 1035)["selected_match_id"] == "cam-2"


def test_emergency_is_disabled_by_default_and_only_bypasses_confirmation_when_enabled():
    extreme = {"cam-1": 0, "cam-2": 1}
    assert evaluate(stable(), 5, 1000, extreme)["selected_match_id"] == "cam-1"
    enabled = stable(emergency_enabled=True)
    assert evaluate(enabled, 5, 1000, extreme)["selection_reason"] == "strong_challenger"
    enabled.manual_select("cam-1", now=1001)
    assert evaluate(enabled, 7, 1002, extreme)["selection_reason"] == "manual_hold"
    enabled.manual_hold_until = 0
    enabled.commit_selection("cam-2", now=1003, kind="automatic")
    assert evaluate(enabled, 9, 1004, {"cam-1": 1, "cam-2": 0})["selection_reason"] == "automatic_cooldown"


def test_unavailable_fallback_requires_auto_enabled_and_valid_scores():
    d = stable()
    snapshot = scores({"cam-2": 0.7})
    snapshot["health"]["cam-1"] = "eof"
    assert d.ingest(snapshot, now=1000, playback_position=5)
    d.set_auto(False)
    assert d.decide(now=1000, playback_position=5) is None
    assert d.current_match_id == "cam-1" and d.latest_scores == {"cam-2": 0.7}
    d.set_auto(True)
    fallback = d.decide(now=1000.1, playback_position=5)
    assert fallback["selected_match_id"] == "cam-2" and fallback["selection_kind"] == "fallback"


def test_history_records_only_changed_commits_and_is_bounded():
    d = stable()
    for index in range(150):
        d.commit_selection("cam-2" if index % 2 == 0 else "cam-1", now=1000 + index,
                           kind="manual", reason="manual_selection", playback_position=index)
    assert len(d.history) == 100 and d.history[0]["id"] == 51 and d.history[-1]["id"] == 150
    previous = d.last_switch_at
    assert not d.commit_selection(d.current_match_id, now=9999, kind="automatic")
    assert d.last_switch_at == previous and len(d.history) == 100
    assert not math.isfinite(d.last_automatic_switch_at)
