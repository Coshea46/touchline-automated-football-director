"""Offline tests for unit tools validation test_replay_probe."""


import pytest


def test_paid_probe_refuses_cloud_without_explicit_backend_monitor(monkeypatch):
    from touchline.tools.validation import replay_probe as probe_replay

    monkeypatch.setenv("MATCHPULSE_MONITOR", "false")
    monkeypatch.setattr("sys.argv", ["probe_replay", "--allow-cloud"])
    with pytest.raises(SystemExit) as result:
        probe_replay.main()
    assert result.value.code == 2


def test_probe_accounts_for_final_window_censoring_and_complete_drain():
    from touchline.tools.validation.replay_probe import delivery_summary

    records = [{"observation_end": end} for end in range(5, 19, 2)]
    original = delivery_summary(records, 20, 20.75)
    assert original["expected_endpoints"] == list(range(5, 20, 2))
    assert original["missing_endpoints"] == [19]
    assert original["censored_endpoints"] == [19]
    assert original["missing_after_drain"] == []
    assert not original["complete"]
    drained = delivery_summary(records, 20, 23.25)
    assert drained["censored_endpoints"] == []
    assert drained["missing_after_drain"] == [19]
    assert drained["undelivered_past_3s"] == [19]
    records.append({"observation_end": 19})
    complete = delivery_summary(records, 20, 23.25)
    assert complete["complete"] and complete["cadence_order_valid"] and complete["drain_complete"]
    assert complete["cadence_seconds"] == [2] * 7


def test_probe_detects_missing_order_duplicates_and_reported_backlog():
    from touchline.tools.validation.replay_probe import delivery_summary, reported_backlog

    results = delivery_summary([{"observation_end": end} for end in [5, 9, 7, 7]], 11, 14.25)
    assert not results["cadence_order_valid"]
    assert not results["complete"]
    assert results["duplicate_endpoints"] == [7]
    assert results["missing_after_drain"] == [11]
    lag = reported_backlog({"cam-1": {"next_sample_index": 270, "queue_samples": 16},
                            "cam-2": {"next_sample_index": 300, "queue_samples": 0}}, 20)
    assert lag["maximum_backlog_seconds"] == 2
    assert lag["maximum_queue_samples"] == 16
    assert lag["streams"]["cam-2"]["backlog_samples"] == 0


def test_probe_capacity_rejects_degraded_cohort_even_with_complete_delivered_cadence():
    from touchline.tools.validation.replay_probe import capacity_result, delivery_summary

    registered = {"cam-1", "cam-2", "cam-3"}
    records = [{"observation_end": end, "scores": dict.fromkeys(registered, 0.5)} for end in (5, 7, 9)]
    cadence = delivery_summary(records, 10, 13.25)
    assert cadence["complete"]
    assert capacity_result(records, registered, cadence_complete=True, errors=[])["passed"]
    records[1]["scores"].pop("cam-3")
    result = capacity_result(records, registered, cadence_complete=cadence["complete"], errors=[])
    assert not result["passed"] and not result["full_source_cohort_complete"]
    assert result["full_source_cohort_count"] == 2 and result["degraded_recommendation_count"] == 1
    assert result["registered_source_count"] == 3
    assert result["recommendation_count"] == 3  # It remains a valid degraded application recommendation.
