"""Private-bucket source authorization and fixture binding without AWS access."""

import copy
from urllib.parse import quote

import pytest

from touchline.config.video_sources import VIDEO_BUCKET, VIDEO_REGION, VIDEO_READER_ROLE
from touchline.match_metadata import metadata_for_source
from touchline.replay.sources import SourceRegistry
from touchline.storage import PresignedObject, StorageAuthorizationError
from touchline.tools.storage.verify_sources import verify_sources


class FakeAccess:
    def __init__(self):
        self.clock = 1000.0
        self.serial = 0
        self.closed = False
        self.heads = []
        self.signatures = []
        self.changed = {}

    def get_client(self):
        return self

    def head_object(self, *, Bucket, Key):
        self.heads.append((Bucket, Key))
        record = metadata_for_source(f"cam-{Key[6:-4]}", Key)
        return {"ETag": self.changed.get(Key, f'"destination-{Key}"'),
                "ContentLength": record["source_identity"]["content_length"], "ContentType": "video/mp4"}

    def _sign(self, bucket, key, ttl, method):
        self.serial += 1
        self.signatures.append((bucket, key, method))
        url = f"https://{bucket}.s3.{VIDEO_REGION}.amazonaws.com/{quote(key)}?PRIVATE_{method}={self.serial}"
        return PresignedObject(url, self.clock + min(ttl, 600), self.clock + 630, min(ttl, 600))

    def presign_get(self, bucket, key, ttl):
        return self._sign(bucket, key, ttl, "GET")

    def presign_head(self, bucket, key, ttl):
        return self._sign(bucket, key, ttl, "HEAD")

    def close(self):
        self.closed = True


def private_registry():
    access = FakeAccess()
    registry = SourceRegistry.from_env({})
    registry._access = access
    registry.now = lambda: access.clock
    return registry, access


def test_application_defaults_use_private_owner_bucket_role_and_seven_distinct_keys():
    registry, access = private_registry()
    assert (registry.bucket, registry.region, registry.access) == (VIDEO_BUCKET, VIDEO_REGION, "presigned")
    assert registry._aws_env["AWS_ROLE_ARN"] == VIDEO_READER_ROLE
    assert access.heads == []  # Configuration/imports never authenticate.
    resolved = registry.resolve()
    assert len(resolved) == 7
    assert len({source.key for source in resolved}) == 7
    assert all(source.metadata and source.public_spec()["fixture_verified"] for source in resolved)
    assert len(access.heads) == 7 and len(access.signatures) == 14
    for source in resolved:
        assert source.head_url != source.source_url
        assert source.offset_seconds == 0
        assert source.public_spec()["url_expires_at"] == 1600
        assert "head_url" not in source.public_spec()
        assert "PRIVATE" not in repr(source)


def test_migration_uses_actual_destination_identity_and_keeps_original_provenance():
    registry, _ = private_registry()
    source = registry.resolve()[0]
    original = metadata_for_source("cam-1", "match-1.mp4")
    assert source.identity == source.metadata["source_identity"]
    assert source.identity != original["source_identity"]
    assert source.metadata["origin_source_identity"] == original["source_identity"]
    assert source.metadata["home_team"] == original["home_team"]
    assert source.metadata["replay"] == original["replay"]
    assert source.metadata["source_binding"]["full_file_checksum_verified"] is False
    assert metadata_for_source("cam-1", "match-1.mp4", VIDEO_BUCKET) is None


def test_rotated_urls_keep_identity_offsets_and_metadata_but_republish_expiry():
    registry, access = private_registry()
    first = registry.resolve()
    access.clock += 541
    assert registry.needs_refresh()
    second = registry.resolve()
    assert first[0].source_url != second[0].source_url
    assert first[0].identity == second[0].identity
    assert first[0].metadata == second[0].metadata
    assert first[0].offset_seconds == second[0].offset_seconds
    assert second[0].expires_at == 2141
    assert not registry.needs_refresh()


def test_destination_size_mismatch_fails_closed_without_partial_registration(monkeypatch):
    registry, access = private_registry()
    original_head = access.head_object

    def wrong_size(**kwargs):
        result = original_head(**kwargs)
        result["ContentLength"] += 1
        return result

    monkeypatch.setattr(access, "head_object", wrong_size)
    with pytest.raises(StorageAuthorizationError, match="source_copy_metadata_mismatch"):
        registry.resolve()
    assert registry._resolved == () and registry._identity_pins == {}


def test_replacing_a_recording_cannot_inherit_fixture_claims_on_refresh():
    registry, access = private_registry()
    before = registry.resolve()
    access.changed["match-2.mp4"] = '"another-recording"'
    with pytest.raises(StorageAuthorizationError, match="source_object_changed"):
        registry.resolve()
    assert registry._resolved == before


def test_unknown_bucket_cannot_inherit_labels_from_a_known_match_id():
    registry, _ = private_registry()
    source = registry.resolve()[0]
    assert metadata_for_source("cam-1", "match-1.mp4", "unrelated-bucket",
                               verified_identity=source.identity) is None
    altered = copy.deepcopy(source.identity)
    altered["source_key"] = "different.mp4"
    assert metadata_for_source("cam-1", "match-1.mp4", VIDEO_BUCKET,
                               verified_identity=altered) is None


def test_aws_failure_does_not_echo_sdk_credentials_or_signed_queries(monkeypatch):
    registry, access = private_registry()

    def fail(**kwargs):
        raise RuntimeError("PRIVATE_TOKEN=do-not-log&X-Amz-Signature=hidden")

    monkeypatch.setattr(access, "head_object", fail)
    with pytest.raises(StorageAuthorizationError) as caught:
        registry.resolve()
    assert str(caught.value) == "source_access_unavailable"
    assert caught.value.__suppress_context__


def test_read_only_verifier_checks_separate_head_and_one_byte_ranges_and_redacts_urls():
    registry, access = private_registry()
    requested = []

    class Response:
        def __init__(self, request):
            self.method = request.get_method()
            key = request.full_url.split("/", 3)[3].split("?", 1)[0]
            original = metadata_for_source(f"cam-{key[6:-4]}", key)["source_identity"]
            self.headers = {"ETag": f'"destination-{key}"', "Content-Length": str(original["content_length"]),
                            "Content-Range": f'bytes 0-0/{original["content_length"]}', "Content-Type": "video/mp4",
                            "Access-Control-Allow-Origin": "http://localhost:3030"}
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def getcode(self): return 206
        def read(self, amount):
            assert amount == 2
            return b"x"

    def opener(request, timeout):
        requested.append(request)
        assert request.get_header("Origin") == "http://localhost:3030"
        if request.get_method() == "HEAD":
            assert "PRIVATE_HEAD=" in request.full_url
        else:
            assert "PRIVATE_GET=" in request.full_url
            assert request.get_header("Range") == "bytes=0-0"
        return Response(request)

    report = verify_sources(registry, opener=opener, now=lambda: access.clock)
    assert report["passed"] and len(report["objects"]) == 7 and len(requested) == 14
    assert access.closed
    assert "PRIVATE_" not in str(report)
    assert report["full_file_checksum_verified"] is False


def test_private_signer_uses_the_registry_region_and_local_credential_chain(monkeypatch):
    access, seen = FakeAccess(), {}

    def create_access(**kwargs):
        seen.update(kwargs)
        return access

    monkeypatch.setattr("touchline.replay.sources.S3Access", create_access)
    registry = SourceRegistry.from_env({"AWS_DEFAULT_REGION": VIDEO_REGION, "AWS_REGION": "us-east-1"})
    registry.resolve()
    assert seen["region"] == VIDEO_REGION
    assert seen["token_provider"] is None
    assert seen["env"]["AWS_ROLE_ARN"] == VIDEO_READER_ROLE
