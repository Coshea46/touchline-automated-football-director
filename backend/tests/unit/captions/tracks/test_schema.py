"""Offline tests for unit captions tracks test_schema."""


import pytest
from touchline.captions.tracks.schema import cue_at
from touchline.captions.identity import source_identity
from touchline.captions.tracks.schema import validate_track
from touchline.captions.tracks.schema import webvtt
from touchline.captions.tracks.pipeline import CaptionPipeline, words_to_track
from touchline.captions.errors import CaptionError
from touchline.captions.identity import canonical_json
from tests.support.captions.tracks import generate_args


def test_identity_excludes_signed_query_but_rejects_replaced_object(identity):
    renewed = source_identity("match-1.mp4", "synthetic-etag", 100,
                              "https://test.invalid/match-1.mp4?signature=renewed")
    assert identity == renewed
    assert identity["digest"] != source_identity("match-1.mp4", "changed", 100,
                                                 "https://test.invalid/match-1.mp4")["digest"]
    assert "private" not in canonical_json(identity)


def test_cue_seek_pause_and_half_open_source_boundaries(track):
    assert cue_at(track, 120.25)["text"] == "synthetic fixture"
    assert cue_at(track, 121.5) is None
    assert cue_at(track, 122.5)["text"] == "second fixture"
    assert cue_at(track, 122.5) == cue_at(track, 122.5)  # paused source clock
    assert cue_at(track, 0) is None
    assert cue_at(track, 151) is None
    assert cue_at(track, 120.3)["text"] == "synthetic fixture"  # backward seek


def test_webvtt_absolute_source_time_and_escaping(track):
    track["cues"][0]["text"] = "<script> & fixture -->"
    result = webvtt(track)
    assert "00:02:00.250 --> 00:02:01.500" in result
    assert "&lt;script&gt; &amp; fixture --&gt;" in result
    assert "not human reviewed" in result


@pytest.mark.parametrize("mutate", [
    lambda t: t["cues"][0].update(start_seconds=float("nan")),
    lambda t: t["cues"][0].update(end_seconds=151),
    lambda t: t["cues"][1].update(start_seconds=121),
    lambda t: t["cues"][0].update(text=""),
    lambda t: t.update(source_identity="unverified"),
    lambda t: t["provenance"].update(timestamp_method="guessed"),
])
def test_track_rejects_bad_timing_or_provenance(track, mutate):
    mutate(track)
    with pytest.raises(ValueError):
        validate_track(track)


def test_identity_cannot_be_reused_for_other_source_url_or_key(tmp_path, identity):
    with pytest.raises(CaptionError, match="source_identity_mismatch"):
        CaptionPipeline(tmp_path).generate(**{**generate_args(identity), "source_url": "https://other.invalid/match-1.mp4"})
    with pytest.raises(CaptionError, match="source_identity_mismatch"):
        CaptionPipeline(tmp_path).generate(**{**generate_args(identity), "source_key": "match-2.mp4"})


def test_scribe_word_grouping_preserves_speakers_events_and_exact_boundaries(identity):
    response = {"text": "TEST FIXTURE (applause) OTHER", "words": [
        {"type": "word", "text": "TEST", "start": .1, "end": .4, "speaker_id": "speaker_0"},
        {"type": "spacing", "text": " ", "start": .4, "end": .5},
        {"type": "word", "text": "FIXTURE", "start": .5, "end": .9, "speaker_id": "speaker_0"},
        {"type": "audio_event", "text": "(applause)", "start": 1, "end": 2},
        {"type": "word", "text": "OTHER", "start": 1.2, "end": 1.6, "speaker_id": "speaker_1"}]}
    result = words_to_track(response, match_id="cam-1", source_key="match-1.mp4", identity=identity["digest"],
                            start=120, end=150, language="en", audio_metadata={})
    assert [cue["text"] for cue in result["cues"]] == ["TEST FIXTURE", "(applause)", "OTHER"]
    assert result["cues"][0]["start_seconds"] == 120.1 and result["cues"][0]["end_seconds"] == 120.9
    assert result["cues"][1]["kind"] == "audio_event" and result["cues"][2]["speaker_id"] == "speaker_1"
    assert result["provenance"]["provider"] == "elevenlabs" and result["provenance"]["timestamp_method"] == "provider_word"
