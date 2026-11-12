"""Private-bucket source authorization and fixture binding without AWS access."""

import copy
from urllib.parse import quote, urlsplit

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
        endpoint = "s3.dualstack" if method == "GET_DUAL" else "s3"
        url = f"https://{bucket}.{endpoint}.{VIDEO_REGION}.amazonaws.com/{quote(key)}?PRIVATE_{method}={self.serial}"
        return PresignedObject(url, self.clock + min(ttl, 600), self.clock + 630, min(ttl, 600))

    def presign_get(self, bucket, key, ttl, *, dualstack=False):
        return self._sign(bucket, key, ttl, "GET_DUAL" if dualstack else "GET")

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


def test_split_browser_origins_balance_seven_sources_and_preserve_canonical_identity_and_worker_urls():
    registry, access = private_registry()
    canonical = registry.resolve()
    registry.media_origins = "split"
    split = registry.resolve()
    assert len(access.signatures) == 14 + 17
    assert [".dualstack." in urlsplit(source.playback_url).hostname for source in split] == [False, True, False, True, False, True, False]
    assert len({urlsplit(source.playback_url).hostname for source in split}) == 2
    for before, after in zip(canonical, split):
        assert before.identity == after.identity and before.metadata == after.metadata
        assert ".dualstack." not in urlsplit(after.source_url).hostname
        assert ".dualstack." not in urlsplit(after.head_url).hostname
        assert after.worker_spec()["source_url"] == after.source_url
        assert after.public_spec()["url"] == after.playback_url
    access.clock += 541
    rotated = registry.resolve()
    for before, after in zip(split, rotated):
        assert before.playback_url != after.playback_url
        assert urlsplit(before.playback_url).hostname == urlsplit(after.playback_url).hostname
        assert before.identity == after.identity and before.metadata == after.metadata


def test_split_minimum_expiry_includes_alternate_get_and_failed_refresh_is_atomic(monkeypatch):
    registry, access = private_registry()
    registry.media_origins = "split"
    original = access.presign_get
    def sign(bucket, key, ttl, *, dualstack=False):
        signed = original(bucket, key, ttl, dualstack=dualstack)
        return PresignedObject(signed.url, access.clock + 90, access.clock + 630, 90) if dualstack else signed
    monkeypatch.setattr(access, "presign_get", sign)
    before = registry.resolve()
    assert [source.expires_at for source in before] == [1600, 1090, 1600, 1090, 1600, 1090, 1600]
    pins = dict(registry._identity_pins)
    def fail(bucket, key, ttl, *, dualstack=False):
        if dualstack and key == "match-4.mp4":
            raise RuntimeError("PRIVATE_SIGNATURE_MUST_NOT_ESCAPE")
        return sign(bucket, key, ttl, dualstack=dualstack)
    monkeypatch.setattr(access, "presign_get", fail)
    with pytest.raises(StorageAuthorizationError, match="^source_access_unavailable$"):
        registry.resolve()
    assert registry._resolved is before and registry._identity_pins == pins


@pytest.mark.parametrize("alter", [
    lambda url: url.replace(".s3.dualstack.", ".s3."),
    lambda url: url.replace(".amazonaws.com/", ".amazonaws.com.evil.invalid/"),
    lambda url: url.replace("/match-2.mp4", "/different.mp4"),
    lambda url: url.replace(".amazonaws.com/", ".amazonaws.com:443/"),
])
def test_split_rejects_any_alternate_signed_locator_that_is_not_exact_same_sdk_bucket_key(monkeypatch, alter):
    registry, access = private_registry()
    registry.media_origins = "split"
    original = access.presign_get
    def sign(bucket, key, ttl, *, dualstack=False):
        signed = original(bucket, key, ttl, dualstack=dualstack)
        return PresignedObject(alter(signed.url), signed.expires_at, signed.authorization_expires_at,
                               signed.actual_ttl_seconds) if dualstack else signed
    monkeypatch.setattr(access, "presign_get", sign)
    with pytest.raises(StorageAuthorizationError, match="^source_playback_locator_mismatch$"):
        registry.resolve()
    assert registry._resolved == () and registry._identity_pins == {}


def test_split_requires_expiry_aware_injected_access_and_never_silently_falls_back(monkeypatch):
    registry, access = private_registry()
    with pytest.raises(ValueError, match="split_media_requires_expiry_aware_access"):
        SourceRegistry(registry.sources, s3_client=access, access="presigned", media_origins="split")
    registry.media_origins = "split"
    monkeypatch.setattr(access, "presign_get", lambda bucket, key, ttl: access._sign(bucket, key, ttl, "GET"))
    with pytest.raises(StorageAuthorizationError, match="^source_access_unavailable$"):
        registry.resolve()
    assert registry._resolved == ()


@pytest.mark.parametrize("mode", ["", "dualstack", "SPLIT", True])
def test_media_origin_configuration_is_explicit_and_invalid_values_fail_before_access(mode):
    with pytest.raises(ValueError, match="^invalid_s3_media_origins$"):
        SourceRegistry.from_env({"MATCHPULSE_S3_MEDIA_ORIGINS": mode})


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


def test_private_heads_overlap_with_bound_four_but_signing_and_publication_remain_ordered():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock, get_ident
    class BlockingAccess(FakeAccess):
        def __init__(self):
            super().__init__()
            self.lock, self.started, self.release = Lock(), Event(), Event()
            self.active = self.maximum = 0
            self.client_threads, self.signing_threads = [], []
        def get_client(self):
            self.client_threads.append(get_ident())
            return self
        def head_object(self, **kwargs):
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                if self.active == 4:
                    self.started.set()
            try:
                assert self.release.wait(3)
                return super().head_object(**kwargs)
            finally:
                with self.lock:
                    self.active -= 1
        def _sign(self, *args):
            self.signing_threads.append(get_ident())
            return super()._sign(*args)
        def close(self):
            assert self.active == 0
            super().close()
    registry, _ = private_registry()
    access = registry._access = BlockingAccess()
    with ThreadPoolExecutor(max_workers=1) as owner:
        future = owner.submit(registry.resolve)
        try:
            assert access.started.wait(2), "HEAD requests did not overlap"
            assert access.maximum == 4 and registry._resolved == () and registry._identity_pins == {}
            assert access.signatures == []
        finally:
            access.release.set()
        resolved = future.result(timeout=2)
        owner.submit(registry.close).result(timeout=2)
    assert access.closed and access.maximum == 4
    assert [source.match_id for source in resolved] == [f"cam-{i}" for i in range(1, 8)]
    assert len(access.client_threads) == 1 and set(access.signing_threads) == set(access.client_threads)
    assert [(key, method) for _, key, method in access.signatures] == [
        (f"match-{i}.mp4", method) for i in range(1, 8) for method in ("GET", "HEAD")]
    metrics = registry.resolve_metrics
    assert metrics["succeeded"] and metrics["head_workers"] == 4 and metrics["scheduled_head_count"] == 7
    assert all(0 <= metrics[name] <= metrics["elapsed_seconds"] for name in
               ("client_setup_seconds", "head_wall_seconds", "presign_seconds"))
    assert "PRIVATE" not in repr(metrics) and "match-" not in repr(metrics)


def test_failed_parallel_head_batch_joins_before_queued_close_and_keeps_previous_complete_identity_set():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock
    class FailingAccess(FakeAccess):
        def __init__(self):
            super().__init__()
            self.lock, self.started, self.failed = Lock(), Event(), Event()
            self.fail_now, self.release = Event(), Event()
            self.block = False
            self.active = 0
        def head_object(self, **kwargs):
            if not self.block:
                return super().head_object(**kwargs)
            with self.lock:
                self.active += 1
                if self.active == 4:
                    self.started.set()
            try:
                if kwargs["Key"] == "match-1.mp4":
                    assert self.fail_now.wait(3)
                    self.failed.set()
                    raise RuntimeError("PRIVATE_TOKEN&X-Amz-Signature=must-not-leak")
                assert self.release.wait(3)
                assert not self.closed
                return super().head_object(**kwargs)
            finally:
                with self.lock:
                    self.active -= 1
        def close(self):
            assert self.active == 0, "SDK client closed while HEAD still used it"
            super().close()
    registry, _ = private_registry()
    access = registry._access = FailingAccess()
    previous, pins = registry.resolve(), dict(registry._identity_pins)
    access.block = True
    with ThreadPoolExecutor(max_workers=1) as owner:
        future = owner.submit(registry.resolve)
        try:
            assert access.started.wait(2)
            access.fail_now.set()
            assert access.failed.wait(2)
            closing = owner.submit(registry.close)
            assert not future.done() and not closing.done() and not access.closed
            assert registry._resolved == previous and registry._identity_pins == pins
        finally:
            access.fail_now.set()
            access.release.set()
        with pytest.raises(StorageAuthorizationError, match="^source_access_unavailable$"):
            future.result(timeout=2)
        closing.result(timeout=2)
    assert access.closed and access.active == 0
    assert registry._resolved == previous and registry._identity_pins == pins
    assert len(access.signatures) == 14  # Failure produced no partly refreshed signatures.
    assert registry.resolve_metrics["succeeded"] is False
    assert "PRIVATE" not in repr(registry.resolve_metrics)
