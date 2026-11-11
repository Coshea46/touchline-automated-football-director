"""Offline tests for unit captions tracks test_service."""


from urllib.error import URLError
from types import SimpleNamespace
import pytest
from touchline.captions.tracks.service import CaptionService
from touchline.captions.identity import canonical_json
from touchline.captions.identity import source_identity


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


def test_private_track_identity_uses_current_method_specific_urls_after_cache_expiry(tmp_path, identity, track):
    (tmp_path / f"cam-1-{identity['digest']}.json").write_text(canonical_json(track), encoding="utf-8")
    clock, requests, provider_reads = [0], [], []

    def source(version, *, key="match-1.mp4"):
        return SimpleNamespace(match_id="cam-1", key=key,
            source_url=f"https://test.invalid/match-1.mp4?GET_PRIVATE={version}",
            head_url=f"https://test.invalid/match-1.mp4?HEAD_PRIVATE={version}")

    original = source(1)
    current = [original]

    def sources():
        provider_reads.append(True)
        return current

    def checker(key, get_url, head_url):
        requests.append((key, get_url, head_url))
        return identity["digest"]

    service = CaptionService(tmp_path, [original], {"cam-1": identity}, identity_checker=checker,
                             source_provider=sources, now=lambda: clock[0])
    first = service.get("cam-1")
    assert first["status"] == "ready"
    current[0] = source(2)
    clock[0] = 299
    assert service.get("cam-1")["status"] == "ready"
    assert len(requests) == len(provider_reads) == 1
    clock[0] = 300
    refreshed = service.get("cam-1")
    assert refreshed["status"] == "ready"
    assert requests[-1] == ("match-1.mp4", current[0].source_url, current[0].head_url)
    assert len(requests) == len(provider_reads) == 2
    assert "GET_PRIVATE" not in canonical_json(refreshed)
    assert "HEAD_PRIVATE" not in canonical_json(refreshed)

    current[0] = source(3, key="different-recording.mp4")
    clock[0] = 600
    assert service.get("cam-1")["reason"] == "source_identity_verification_unavailable"
    assert len(requests) == 2


def test_private_track_records_can_supply_a_head_url_without_a_dynamic_provider(tmp_path, identity, track):
    (tmp_path / f"cam-1-{identity['digest']}.json").write_text(canonical_json(track), encoding="utf-8")
    calls = []
    records = {"cam-1": {"source_key": "match-1.mp4", "source_url": "https://test.invalid/match-1.mp4?GET=PRIVATE",
                         "source_identity_url": "https://test.invalid/match-1.mp4?HEAD=PRIVATE"}}

    def checker(*args):
        calls.append(args)
        return identity["digest"]

    service = CaptionService(tmp_path, records, {"cam-1": identity}, identity_checker=checker)
    assert service.get("cam-1")["status"] == "ready"
    assert calls == [("match-1.mp4", records["cam-1"]["source_url"], records["cam-1"]["source_identity_url"])]


@pytest.mark.parametrize("identity_location", ["source", "metadata"])
def test_metadata_track_lookup_uses_the_verified_current_object_identity(tmp_path, identity, track, identity_location):
    catalog = tmp_path / "matches.json"
    catalog.write_text(canonical_json({"matches": [{"match_id": "cam-1", "source_key": "match-1.mp4",
                                                   "source_identity": identity}]}), encoding="utf-8")
    get_url = "https://private.invalid/match-1.mp4?GET=PRIVATE"
    current = source_identity("match-1.mp4", "current-private-etag", 100, get_url)
    source = SimpleNamespace(match_id="cam-1", key="match-1.mp4", source_url=get_url,
        head_url="https://private.invalid/match-1.mp4?HEAD=PRIVATE",
        identity=current if identity_location == "source" else None,
        metadata={"source_identity": current if identity_location == "metadata" else identity})
    # A one-shot source iterable must not lose its URLs during construction.
    service = CaptionService.from_metadata(catalog, track_dir=tmp_path, sources=iter([source]),
                                           identity_checker=lambda *args: current["digest"])
    (tmp_path / f"cam-1-{identity['digest']}.json").write_text(canonical_json(track), encoding="utf-8")
    assert service.identities["cam-1"] == current
    assert service.get("cam-1")["reason"] == "missing_track"
    assert service.get("cam-1", source_identity=identity["digest"])["status"] == "source_mismatch"

    new_track = {**track, "source_identity": current["digest"]}
    (tmp_path / f"cam-1-{current['digest']}.json").write_text(canonical_json(new_track), encoding="utf-8")
    assert service.get("cam-1", source_identity=current["digest"])["status"] == "ready"


def test_catalog_identity_does_not_follow_a_matching_key_into_an_unverified_bucket(tmp_path, identity):
    catalog = tmp_path / "matches.json"
    catalog.write_text(canonical_json({"matches": [{"match_id": "cam-1", "source_key": "match-1.mp4",
                                                   "source_identity": identity}]}), encoding="utf-8")
    source = SimpleNamespace(match_id="cam-1", key="match-1.mp4",
                             source_url="https://private.invalid/match-1.mp4?GET=PRIVATE")
    service = CaptionService.from_metadata(catalog, sources=[source],
        identity_checker=lambda *args: pytest.fail("An unverified identity must not issue HEAD"))
    assert service.get("cam-1")["reason"] == "source_identity_unverified"
