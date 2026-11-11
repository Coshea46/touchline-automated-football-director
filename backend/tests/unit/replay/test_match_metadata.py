"""Offline tests for unit replay test_match_metadata."""


import pytest
from touchline.match_metadata import metadata_for_source, replay_score_at
from touchline.replay import MatchSource, SourceRegistry


def test_shared_fixture_catalog_preserves_seven_distinct_original_mappings():
    registry = SourceRegistry.from_env({})
    resolved = registry.resolve()
    assert len({source.source_url for source in resolved}) == 7
    assert resolved[0].label == "Chelsea vs Swansea City"
    assert resolved[6].label == "Swansea City vs Manchester United"
    assert [source.key for source in resolved] == [f"match-{i}.mp4" for i in range(1, 8)]
    assert all(source.public_spec()["source_identity"] == source.metadata["source_identity"]["digest"]
               for source in resolved)


@pytest.mark.parametrize("seconds", [0, 119.99, 600, 1000, float("nan"), True])
def test_replay_scores_are_withheld_outside_verified_source_interval(seconds):
    record = metadata_for_source("cam-1", "match-1.mp4")
    assert replay_score_at(record, seconds) is None


def test_real_liverpool_score_change_has_no_invented_boundary():
    record = metadata_for_source("cam-5", "match-5.mp4")
    assert replay_score_at(record, 120) == {"home": 0, "away": 0, "kind": "replay_metadata"}
    assert replay_score_at(record, 149.99)["away"] == 0
    assert replay_score_at(record, 150) is None
    assert replay_score_at(record, 159.99) is None
    assert replay_score_at(record, 160)["away"] == 1
    assert replay_score_at(record, 600) is None


def test_unknown_custom_source_never_inherits_an_existing_id_fixture_or_score():
    assert metadata_for_source("cam-1", "different.mp4") is None
    assert metadata_for_source("cam-1", "match-1.mp4", "different-bucket") is None
    custom = SourceRegistry([MatchSource("cam-1", "Custom recording", source_url="https://example.com/custom.mp4")])
    spec = custom.resolve()[0].public_spec()
    assert spec["label"] == "Custom recording" and "metadata" not in spec and "source_identity" not in spec


def test_metadata_copies_cannot_contaminate_other_sources():
    one = metadata_for_source("cam-1", "match-1.mp4")
    one["replay"]["score_intervals"][0]["away"] = 99
    assert metadata_for_source("cam-1", "match-1.mp4")["replay"]["score_intervals"][0]["away"] == 0
