"""Offline tests for unit tools evaluation test_director."""


import copy
import numpy as np
import pytest
from touchline.tools.evaluation.director import grouped_events, measure, reference_trace, replay_director, validate_rows
from touchline.replay.director import DIRECTOR_MODES


def fixture(seconds=35, score=None):
    rows, values = [], []
    for endpoint in range(5, seconds, 2):
        for match in ("a", "b"):
            rows.append({"window_id": f"{match}-{endpoint}", "match_id": match, "half": 1,
                         "split": "valid", "observation_start_seconds": endpoint - 5,
                         "observation_end_seconds": endpoint})
            values.append(score(match, endpoint) if score else (0.05 if match == "a" else 0.95))
    return rows, np.asarray(values), {("a", 1): seconds, ("b", 1): seconds}


def event(match, timestamp, kind="Goal", group="g"):
    return {"event_id": f"e-{match}-{timestamp}", "match_id": match, "half": 1,
            "split": "valid", "qualifying": True, "event_type": kind,
            "timestamp_seconds": timestamp, "evaluation_group_id": group}


@pytest.mark.parametrize(("mode", "first_end"), [("all_action", 5), ("balanced", 7), ("relaxed", 9)])
def test_real_mode_confirmations_and_ack_commit(mode, first_end):
    result = replay_director(*fixture(), mode=mode, delay=1, ack_delay=0.25)
    switch = result["switches"][0]
    assert switch["observation_end"] == first_end
    assert switch["emitted_at"] == first_end + 1
    assert switch["start"] == first_end + 1.25
    assert result["sessions"][0]["segments"][0]["end"] == switch["start"]
    assert result["rejected_records"] == 0


def test_timer_can_switch_on_cached_cohort_after_actual_ack_cooldown():
    def scores(match, endpoint):
        winner = "b" if endpoint == 5 else "a"
        return 0.95 if match == winner else 0.05

    result = replay_director(*fixture(score=scores), mode="all_action", delay=0, ack_delay=0.25)
    first, second = result["switches"][:2]
    assert first["start"] == 5.25
    assert second["observation_end"] == 9
    assert second["emitted_at"] == 9.3  # 100ms session timer after cooldown at9.25.
    assert second["start"] == 9.55
    assert second["start"] - first["start"] >= DIRECTOR_MODES["all_action"].cooldown_seconds


def test_late_arrivals_rejected_instead_of_backdated_or_missing_as_zero():
    result = replay_director(*fixture(), mode="all_action", delay=4)
    assert result["rejected_records"] > 0
    assert result["switches"] == []
    assert result["sessions"][0]["segments"][0]["match_id"] == "a"


def test_display_ack_expiry_prevents_switch_and_cooldown():
    result = replay_director(*fixture(), mode="all_action", delay=2.157, ack_delay=1)
    assert result["switches"] == []  # Observation is older than3s by display time.
    assert result["rejected_records"] == 0
    assert result["rejected_acks"] > 0


def test_realistic_delay_changes_display_time_without_looking_ahead():
    early = replay_director(*fixture(), mode="balanced", delay=0)
    late = replay_director(*fixture(), mode="balanced", delay=2.157)
    assert early["switches"][0]["start"] == 7.25
    assert late["switches"][0]["start"] == pytest.approx(9.407)
    assert all(s["start"] >= s["observation_end"] + 2.157 for s in late["switches"])


@pytest.mark.parametrize("change", ["train", "duplicate", "nan", "future_window", "off_grid"])
def test_invalid_validation_inputs_fail_closed(change):
    rows, scores, _ = fixture()
    if change == "train":
        rows[0]["split"] = "train"
    elif change == "duplicate":
        rows[1] = rows[0].copy()
    elif change == "nan":
        scores[0] = np.nan
    elif change == "future_window":
        rows[0]["observation_start_seconds"] += 1
    elif change == "off_grid":
        rows[0]["observation_start_seconds"] += 0.5
        rows[0]["observation_end_seconds"] += 0.5
    with pytest.raises(ValueError):
        validate_rows(rows, scores)


def test_exact_goal_timestamp_is_not_replaced_by_earlier_group_member():
    events = [event("a", 10, "Shots on target"), event("a", 12, "Goal")]
    assert grouped_events(events)[0]["timestamp"] == 10
    assert grouped_events(events, {"Goal"})[0]["timestamp"] == 12
    events[0]["split"] = "test"
    with pytest.raises(ValueError):
        grouped_events(events)


def test_events_already_watching_advance_switch_and_miss_are_separate():
    trace = replay_director(*fixture(seconds=40), mode="all_action", delay=0, ack_delay=0.25)
    before = copy.deepcopy(trace)
    events = [event("b", 10, group="advance"), event("b", 25, group="already"),
              event("a", 26, group="missed")]
    metrics = measure(trace, events, start=7.407)
    goals = metrics["scopes"]["goals"]
    assert (goals["events"], goals["shown"], goals["missed"]) == (3, 2, 1)
    assert goals["advance_switches_within_10s"] == 1
    assert goals["newly_anticipated_3_to_10s"] == 1
    assert goals["already_viewing_before_10s_warning_window"] == 1
    assert goals["mean_warning_seconds_for_recent_advance_switches"] == 4.75
    assert trace == before  # Annotations cannot change any policy decision.


def test_original_half_sessions_reset_without_becoming_independent_matches():
    rows, scores, durations = fixture()
    other = [{**row, "half": 2} for row in rows]
    durations.update({(match, 2): seconds for (match, _), seconds in list(durations.items())})
    result = replay_director(rows + other, np.concatenate([scores, scores]), durations, mode="balanced")
    assert [s["start"] for s in result["switches"]] == [8.25, 8.25]
    assert [session["segments"][0]["match_id"] for session in result["sessions"]] == ["a", "a"]


def test_reference_dwell_and_common_footage_denominator():
    durations = {("a", 1): 95, ("b", 1): 100}
    trace = reference_trace(["a", "b"], durations, "round_robin_30s")
    assert [s["match_id"] for s in trace["sessions"][0]["segments"]] == ["a", "b", "a", "b"]
    metrics = measure(trace, [], start=5)
    assert metrics["viewing_seconds"] == 90
    assert metrics["switches"] == 3
    assert metrics["mean_viewing_duration_seconds"] == 22.5
