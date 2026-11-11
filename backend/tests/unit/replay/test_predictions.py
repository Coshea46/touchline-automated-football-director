"""Offline tests for unit replay test_predictions."""


import pytest
from tests.support.replay.session import scores, director


def test_director_stay_bonus_raw_scores_and_deterministic_ties():
    d = director()
    assert d.ingest(scores({"cam-1": 0.4, "cam-2": 0.5}), now=1000, playback_position=5)
    first = d.decide(now=1000, playback_position=5)
    assert first["selected_match_id"] == "cam-1"
    assert first["scores"]["cam-1"] == 0.4
    assert d.ingest(scores({"cam-1": 0.4, "cam-2": 0.55}, end=7, sequence=2),
                    now=1000, playback_position=7)
    assert d.decide(now=1000, playback_position=7)["selected_match_id"] == "cam-2"
    # The bonus is applied once to comparison; stored scores remain unmodified.
    assert d.records["cam-1"][-1].value == 0.4


def test_director_reports_post_compute_delivery_separately_from_worker_compute():
    d = director()
    assert d.ingest(scores(now=999.5), now=1000, playback_position=5)
    decision = d.decide(now=1000, playback_position=5)
    assert decision["worker_completion_to_backend_seconds"] == 0.5
    assert decision["latency_seconds"] == pytest.approx(0.7)


def test_director_switch_interval_manual_hold_and_auto_disable():
    d = director()
    d.ingest(scores(), now=1000, playback_position=5)
    assert d.decide(now=1000, playback_position=5)["selected_match_id"] == "cam-2"
    d.ingest(scores({"cam-1": 1, "cam-2": 0}, end=7, sequence=2, now=1001),
             now=1001, playback_position=7)
    assert d.decide(now=1001, playback_position=7)["selected_match_id"] == "cam-2"
    d.manual_select("cam-1", now=1002)
    d.ingest(scores(end=9, sequence=3, now=1003), now=1003, playback_position=9)
    assert d.decide(now=1003, playback_position=9)["selected_match_id"] == "cam-1"
    d.auto_enabled = False
    d.ingest(scores(end=11, sequence=4, now=1033), now=1033, playback_position=11)
    assert d.decide(now=1033, playback_position=11)["selected_match_id"] == "cam-1"
    d.auto_enabled = True
    d.ingest(scores(end=13, sequence=5, now=1035), now=1035, playback_position=13)
    assert d.decide(now=1035, playback_position=13)["selected_match_id"] == "cam-2"


@pytest.mark.parametrize("field,value", [("score", float("nan")), ("score", -0.1), ("score", 1.1),
                                        ("sequence", True), ("match_id", "unknown"),
                                        ("session_id", "previous-session"), ("model_version", "old"),
                                        ("observation_start", 1), ("produced_at", 990),
                                        ("produced_at", 1002), ("latency_seconds", 4),
                                        ("clock_epoch", 1)])
def test_invalid_records_never_trigger_incomplete_cohort_switch(field, value):
    d = director()
    snapshot = scores()
    snapshot["scores"][1][field] = value
    d.ingest(snapshot, now=1000, playback_position=5)
    assert d.decide(now=1000, playback_position=5) is None
    assert d.current_match_id == "cam-1"


@pytest.mark.parametrize("field,value", [("session_id", "old"), ("model_version", "old"),
                                        ("worker_sequence", -1), ("worker_sequence", True),
                                        ("clock_epoch", 1), ("score_kind", "model")])
def test_old_or_wrong_snapshot_rejected(field, value):
    d = director()
    snapshot = scores()
    snapshot[field] = value
    assert not d.ingest(snapshot, now=1000, playback_position=5)


def test_future_stale_out_of_order_and_repeated_windows_rejected():
    d = director()
    assert not d.ingest(scores(), now=1000, playback_position=4.9)
    d = director()
    assert not d.ingest(scores(), now=1000, playback_position=9)
    d = director()
    assert d.ingest(scores(), now=1000, playback_position=5)
    assert not d.ingest(scores(), now=1000, playback_position=5)
    out_of_order = scores(sequence=2)
    assert not d.ingest(out_of_order, now=1000, playback_position=5)
    assert d.decide(now=1000, playback_position=5)
    assert d.decide(now=1000, playback_position=5) is None


def test_partial_cohort_waits_and_unavailable_stream_is_excluded():
    d = director()
    assert d.ingest(scores({"cam-1": 0.2}), now=1000, playback_position=5)
    assert d.decide(now=1000, playback_position=5) is None
    snapshot = scores({"cam-2": 0.9}, sequence=2)
    d.ingest(snapshot, now=1000, playback_position=5)
    assert d.decide(now=1000, playback_position=5)["selected_match_id"] == "cam-2"
    snapshot = scores({"cam-1": 0.6}, end=7, sequence=3)
    snapshot["health"]["cam-2"] = {"status": "eof"}
    d.ingest(snapshot, now=1002, playback_position=7)
    assert d.decide(now=1002, playback_position=7)["selected_match_id"] == "cam-1"


def test_cache_is_bounded_and_seek_clears_previous_epoch():
    d = director()
    for sequence in range(1, 20):
        end = 3 + sequence * 2
        d.ingest(scores(end=end, sequence=sequence), now=1000, playback_position=end)
    assert all(len(records) == 2 for records in d.records.values())
    d.reset(1)
    assert not any(d.records.values())
    assert not d.ingest(scores(), now=1000, playback_position=5)
    assert d.ingest(scores(epoch=1), now=1000, playback_position=5)
