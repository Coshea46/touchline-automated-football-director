"""Offline tests for unit captions tracks test_service."""


from urllib.error import URLError
import pytest
from touchline.captions.tracks.service import CaptionService
from touchline.captions.identity import canonical_json


def test_service_identity_isolation_and_immediate_source_switch(tmp_path, identity, track):
    (tmp_path / f"cam-1-{identity['digest']}.json").write_text(canonical_json(track), encoding="utf-8")
    service = CaptionService(tmp_path, {"cam-1": "match-1.mp4", "cam-2": "match-2.mp4"},
                             {"cam-1": identity}, identity_checker=lambda *args: identity)
    assert service.get("cam-1")["status"] == "ready"
    assert service.get("cam-2")["cues"] == []
    assert service.get("cam-1", source_key="override.mp4")["status"] == "source_mismatch"
    assert service.get("cam-1", source_identity="old")["cues"] == []
    assert service.get("../cam-1")["reason"] == "unknown_source"
    changed = CaptionService(tmp_path, {"cam-1": "match-1.mp4"}, {"cam-1": "b" * 64})
    assert changed.get("cam-1")["cues"] == []
    untrusted = CaptionService(tmp_path, {"cam-1": "match-1.mp4"})
    assert untrusted.get("cam-1", source_identity=identity["digest"])["cues"] == []


def test_corrupt_track_is_honest_unavailable(tmp_path, identity):
    (tmp_path / f"cam-1-{identity['digest']}.json").write_text("{}", encoding="utf-8")
    result = CaptionService(tmp_path, {"cam-1": "match-1.mp4"}, {"cam-1": identity}).get("cam-1")
    assert result["reason"] == "invalid_track" and result["cues"] == []


def test_ready_track_revalidates_live_source_identity_with_bounded_cache(tmp_path, identity, track):
    (tmp_path / f"cam-1-{identity['digest']}.json").write_text(canonical_json(track), encoding="utf-8")
    clock, calls = [0], []
    current = [identity]
    def checker(key, url):
        calls.append((key, url))
        return current[0]
    service = CaptionService(tmp_path, {"cam-1": {"source_key": "match-1.mp4",
                                               "source_url": "https://test.invalid/match-1.mp4"}},
                             {"cam-1": identity}, identity_checker=checker, now=lambda: clock[0])
    assert service.get("cam-1")["status"] == "ready"
    assert calls == [("match-1.mp4", "https://test.invalid/match-1.mp4")]
    current[0] = "b" * 64
    clock[0] = 299
    assert service.get("cam-1")["status"] == "ready" and len(calls) == 1
    clock[0] = 300
    result = service.get("cam-1")
    assert result["status"] == "source_mismatch" and result["cues"] == [] and len(calls) == 2
    current[0] = None
    clock[0] = 600
    assert service.get("cam-1")["reason"] == "source_identity_verification_unavailable"


def test_missing_track_never_checks_remote_object(tmp_path, identity):
    service = CaptionService(tmp_path, {"cam-1": "match-1.mp4"}, {"cam-1": identity},
                             identity_checker=lambda *a: pytest.fail("missing track must not issue HEAD"))
    assert service.get("cam-1")["reason"] == "missing_track"


def test_live_head_uses_only_headers_and_sanitizes_failure(monkeypatch, identity):
    from touchline.captions import identity as source_identity_module

    calls = []
    class HeadResponse:
        def __init__(self):
            self.headers = {"ETag": '"synthetic-etag"', "Content-Length": "100"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def open_head(request, timeout):
        calls.append((request.get_method(), timeout))
        return HeadResponse()
    monkeypatch.setattr(source_identity_module, "urlopen", open_head)
    assert source_identity_module.verify_source_identity("match-1.mp4", "https://test.invalid/match-1.mp4") == identity["digest"]
    assert calls == [("HEAD", 4)]
    def fail(*args, **kwargs):
        raise URLError("PRIVATE_SIGNED_URL")
    monkeypatch.setattr(source_identity_module, "urlopen", fail)
    assert source_identity_module.verify_source_identity("match-1.mp4", "https://test.invalid/private") is None


def test_ready_identity_deadline_is_not_renewed_by_cached_get(tmp_path, identity, track):
    (tmp_path / f"cam-1-{identity['digest']}.json").write_text(canonical_json(track), encoding="utf-8")
    clock, wall, checks = [0], [1000], []
    def checker(*args):
        checks.append(True)
        return identity
    service = CaptionService(tmp_path, {"cam-1": "match-1.mp4"}, {"cam-1": identity},
                             identity_checker=checker, now=lambda: clock[0], wall_now=lambda: wall[0])
    first = service.get("cam-1")
    assert first["source_identity_valid_until"] == 1300
    assert first["source_identity_revalidate_after_seconds"] == 300
    clock[0], wall[0] = 299, 1299
    cached = service.get("cam-1")
    assert cached["source_identity_valid_until"] == first["source_identity_valid_until"]
    assert cached["source_identity_revalidate_after_seconds"] == 1
    assert len(checks) == 1
    clock[0], wall[0] = 300, 1300
    refreshed = service.get("cam-1")
    assert refreshed["source_identity_valid_until"] == 1600 and len(checks) == 2
