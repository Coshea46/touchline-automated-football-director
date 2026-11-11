"""Offline tests for integration captions tracks test_pipeline."""


import json
import pytest
from touchline.captions.tracks.service import CaptionService
from touchline.captions.tracks.schema import cue_at
from touchline.captions.tracks.schema import validate_track
from touchline.captions.tracks.pipeline import CaptionPipeline
from touchline.captions.errors import CaptionError
from touchline.captions.tracks.provider import estimate_cost
from touchline.captions.identity import canonical_json
from tests.support.captions.tracks import FakeTranscriber, generate_args


def test_dry_run_cost_never_decodes_uploads_or_writes(tmp_path, identity):
    def forbidden(*a, **k):
        pytest.fail("dry run must have no media/provider side effects")
    pipeline = CaptionPipeline(tmp_path, decoder=forbidden)
    result = pipeline.generate(**generate_args(identity))
    assert result["status"] == "estimate"
    assert result["estimated_cost_usd"] == pytest.approx(30 / 3600 * .22)
    assert estimate_cost(30, "scribe_v2") == pytest.approx(.00183333333333)
    assert list(tmp_path.iterdir()) == []


def test_completed_job_reuses_track_and_never_pays_twice(tmp_path, identity):
    transcriber = FakeTranscriber()
    pipeline = CaptionPipeline(tmp_path, transcriber=transcriber, decoder=lambda *a, **k: (b"fixture-wav", {}))
    assert pipeline.generate(**generate_args(identity), execute=True)["status"] == "complete"
    assert pipeline.generate(**generate_args(identity), execute=True)["status"] == "reused"
    assert transcriber.calls == 1
    service = CaptionService(tmp_path, {"cam-1": "match-1.mp4"}, {"cam-1": identity},
                             identity_checker=lambda *args: identity)
    assert service.get("cam-1")["cues"][0]["start_seconds"] == 120
    assert len(list(tmp_path.glob("*.vtt"))) == 1


def test_failed_uncertain_request_stays_reserved_without_retry(tmp_path, identity):
    transcriber = FakeTranscriber(fail=True)
    pipeline = CaptionPipeline(tmp_path, transcriber=transcriber, decoder=lambda *a, **k: (b"fixture-wav", {}))
    with pytest.raises(CaptionError, match="provider_transport_error"):
        pipeline.generate(**generate_args(identity), execute=True)
    with pytest.raises(CaptionError, match="already_reserved"):
        pipeline.generate(**generate_args(identity), execute=True)
    assert transcriber.calls == 1
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert next(iter(ledger["jobs"].values()))["status"] == "failed_reserved"


def test_budget_and_unsupported_models_reject_before_upload(tmp_path, identity):
    transcriber = FakeTranscriber()
    pipeline = CaptionPipeline(tmp_path, transcriber=transcriber, decoder=lambda *a, **k: (b"fixture-wav", {}))
    with pytest.raises(CaptionError, match="duration_budget"):
        pipeline.generate(**{**generate_args(identity), "end_seconds": 151}, execute=True)
    with pytest.raises(CaptionError, match="cost_budget"):
        pipeline.generate(**generate_args(identity), max_cost_usd=.001, execute=True)
    with pytest.raises(CaptionError, match="unsupported_model"):
        pipeline.generate(**generate_args(identity), model="unsupported", execute=True)
    assert transcriber.calls == 0 and list(tmp_path.iterdir()) == []


def test_missing_key_no_decode_or_write(tmp_path, identity, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(CaptionError, match="api_key_missing"):
        CaptionPipeline(tmp_path).generate(**generate_args(identity), execute=True)
    assert list(tmp_path.iterdir()) == []


def test_two_precomputed_intervals_keep_real_gap_and_accumulate_budget(tmp_path, identity):
    pipeline = CaptionPipeline(tmp_path, transcriber=FakeTranscriber(), decoder=lambda *a, **k: (b"fixture-wav", {}))
    pipeline.generate(**generate_args(identity), max_cost_usd=.004, execute=True)
    pipeline.generate(**{**generate_args(identity), "start_seconds": 180, "end_seconds": 210},
                      max_cost_usd=.004, execute=True)
    result = CaptionService(tmp_path, {"cam-1": "match-1.mp4"}, {"cam-1": identity},
                            identity_checker=lambda *args: identity).get("cam-1")
    assert len(result["coverage_intervals"]) == 2
    assert cue_at(result, 155) is None
    assert cue_at(result, 180)["text"] == "fixture"
    with pytest.raises(CaptionError, match="cumulative_cost_budget"):
        pipeline.generate(**{**generate_args(identity), "start_seconds": 240, "end_seconds": 270},
                          max_cost_usd=.004, execute=True)


def test_completed_track_reuses_without_key_decode_or_upload(tmp_path, identity, monkeypatch):
    transcriber = FakeTranscriber()
    CaptionPipeline(tmp_path, transcriber=transcriber, decoder=lambda *a, **k: (b"fixture-wav", {})).generate(
        **generate_args(identity), execute=True)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    def forbidden(*a, **k):
        pytest.fail("completed track must not decode or upload")
    pipeline = CaptionPipeline(tmp_path, decoder=forbidden)
    result = pipeline.generate(**generate_args(identity), execute=True)
    assert result["status"] == "reused" and transcriber.calls == 1


@pytest.mark.parametrize("changes,reason", [
    ({"match_id": "cam-2"}, "completed_track_identity_mismatch"),
    ({"source_key": "match-2.mp4"}, "completed_track_identity_mismatch"),
    ({"source_identity": "b" * 64}, "completed_track_identity_mismatch"),
    ({"language": "fr", "cues": []}, "completed_track_identity_mismatch"),
    ({"coverage_intervals": [{"start_seconds": 120, "end_seconds": 149}]}, "completed_track_coverage_mismatch"),
    ({"coverage_intervals": [{"start_seconds": 120, "end_seconds": 121},
                             {"start_seconds": 122, "end_seconds": 150}]}, "completed_track_coverage_mismatch"),
])
def test_completed_reuse_rejects_foreign_or_incomplete_valid_track(tmp_path, identity, monkeypatch, changes, reason):
    transcriber = FakeTranscriber()
    CaptionPipeline(tmp_path, transcriber=transcriber, decoder=lambda *a, **k: (b"fixture-wav", {})).generate(
        **generate_args(identity), execute=True)
    path = tmp_path / f"cam-1-{identity['digest']}.json"
    track = json.loads(path.read_text(encoding="utf-8"))
    track.update(changes)
    validate_track(track)  # well-formed foreign data must still fail reuse
    path.write_text(canonical_json(track), encoding="utf-8")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(CaptionError, match=f"^{reason}$"):
        CaptionPipeline(tmp_path).generate(**generate_args(identity), execute=True)
    assert transcriber.calls == 1
