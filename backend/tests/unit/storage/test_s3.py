from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import parse_qs, quote, urlsplit

from botocore import UNSIGNED
from botocore.credentials import Credentials, ReadOnlyCredentials
import pytest

from touchline.storage import S3Access, StorageAuthorizationError


from tests.support.storage import ROLE, BUCKET, Clock, FakeClient, FakeSession, FakeSTS, oidc_access, response


def test_real_sdk_presigns_regional_host_without_redirect_or_network():
    import boto3

    def session_factory(**kwargs):
        return boto3.Session(aws_access_key_id="OFFLINE_TEST_ACCESS",
                             aws_secret_access_key="OFFLINE_TEST_SECRET", **kwargs)

    access = S3Access({}, region="eu-west-2", session_factory=session_factory)
    try:
        for sign in (access.presign_get, access.presign_head):
            parsed = urlsplit(sign(BUCKET, "match-1.mp4").url)
            assert parsed.hostname == f"{BUCKET}.s3.eu-west-2.amazonaws.com"
            assert parsed.path == "/match-1.mp4"
            scope = parse_qs(parsed.query)["X-Amz-Credential"][0]
            assert "/eu-west-2/s3/aws4_request" in scope
    finally:
        access.close()


@pytest.mark.parametrize("region,suffix", [("eu-west-2", "amazonaws.com"), ("cn-north-1", "amazonaws.com.cn")])
def test_real_sdk_signs_dualstack_same_encoded_object_without_network_or_url_rewriting(region, suffix):
    import boto3
    def session_factory(**kwargs):
        return boto3.Session(aws_access_key_id="OFFLINE_TEST_ACCESS",
                             aws_secret_access_key="OFFLINE_TEST_SECRET", **kwargs)
    access = S3Access({}, region=region, session_factory=session_factory)
    key = "nested/football +?#é.mp4"
    try:
        signatures = []
        for dualstack in (False, True):
            for sign in (access.presign_get, access.presign_head):
                result = sign(BUCKET, key, 300, dualstack=dualstack)
                parsed, query = urlsplit(result.url), parse_qs(urlsplit(result.url).query)
                prefix = "s3.dualstack" if dualstack else "s3"
                assert parsed.netloc == f"{BUCKET}.{prefix}.{region}.{suffix}"
                assert parsed.path == "/" + quote(key, safe="/")
                assert query["X-Amz-Expires"] == ["300"]
                assert f"/{region}/s3/aws4_request" in query["X-Amz-Credential"][0]
                assert query["X-Amz-SignedHeaders"] == ["host"]
                signatures.append(query["X-Amz-Signature"][0])
        assert len(set(signatures)) == 4  # Endpoint and method both bind signatures.
        assert len(access._signers) == 2
    finally:
        access.close()


def test_split_warm_rotates_both_cached_signers_with_same_credential_expiry_and_closes_all_once():
    clock = Clock()
    env = {"MODAL_IDENTITY_TOKEN": "PRIVATE_TEST_OIDC_ONE", "AWS_ROLE_ARN": ROLE,
           "MATCHPULSE_S3_MEDIA_ORIGINS": "split"}
    sts = FakeSTS([response(clock, lifetime=1200), lambda: response(clock, suffix="two")])
    access, _, sessions, _, _ = oidc_access(clock, sts, env)
    access.warm()
    head, normal, alternate = sessions[0].clients
    assert len(sts.calls) == 1 and not normal.calls and not alternate.calls
    assert ".dualstack." not in normal.kwargs["endpoint_url"]
    assert alternate.kwargs["endpoint_url"] == "https://s3.dualstack.eu-west-2.amazonaws.com"
    assert alternate.kwargs["config"].use_dualstack_endpoint is True
    first = [access.presign_get(BUCKET, "match-1.mp4", dualstack=mode) for mode in (False, True)]
    assert all(value.actual_ttl_seconds == 1170 and value.authorization_expires_at == clock() + 1200 for value in first)
    clock.value += 600
    env["MODAL_IDENTITY_TOKEN"] = "PRIVATE_TEST_ROTATED_OIDC"
    second = [access.presign_head(BUCKET, "match-1.mp4", dualstack=mode) for mode in (False, True)]
    assert all(value.actual_ttl_seconds == 3570 and value.authorization_expires_at == clock() + 3600 for value in second)
    assert len(sts.calls) == 2 and len(sessions[0].clients) == 5
    assert normal.close_calls == alternate.close_calls == 1 and not head.closed
    assert all(client.kwargs["aws_access_key_id"] == "TEST_ACCESS_two" for client in sessions[0].clients[-2:])
    access.retain()
    assert access.close() is False and not head.closed
    access.release()
    assert access.closed and all(client.close_calls == 1 for client in sessions[0].clients)
    assert access.close() is True


def test_parallel_both_origins_take_refreshable_snapshot_outside_owner_lock_and_bound_client_count():
    owner = []
    class CheckedCredentials(Credentials):
        def get_frozen_credentials(self):
            assert not owner[0]._lock._is_owned()
            return super().get_frozen_credentials()
    session = FakeSession(CheckedCredentials("OFFLINE_ACCESS", "OFFLINE_SECRET"))
    access = S3Access({"MATCHPULSE_S3_MEDIA_ORIGINS": "split"}, session_factory=lambda **kwargs: session)
    owner.append(access)
    access.warm()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda index: access.presign_get(BUCKET, "match-1.mp4", dualstack=bool(index % 2)), range(16)))
    assert len(results) == 16 and len(session.clients) == 3 and len(access._signers) == 2
    access.close()
    assert all(client.close_calls == 1 for client in session.clients)


@pytest.mark.parametrize("mode", ["", "dualstack", "SPLIT", True])
def test_invalid_origin_mode_fails_without_resolving_credentials(mode):
    with pytest.raises(ValueError, match="^invalid_s3_media_origins$"):
        S3Access({"MATCHPULSE_S3_MEDIA_ORIGINS": mode}, session_factory=lambda **_: pytest.fail("AWS must stay lazy"))


@pytest.mark.parametrize("mode", [None, "true", 1])
def test_dualstack_signing_option_requires_explicit_bool(mode):
    access = S3Access({}, session_factory=lambda **_: pytest.fail("AWS must stay lazy"))
    with pytest.raises(ValueError, match="^invalid_s3_dualstack_mode$"):
        access.presign_get(BUCKET, "match-1.mp4", dualstack=mode)


def test_oidc_initialization_is_lazy_and_never_uses_the_local_credential_chain():
    clock = Clock()
    sts = FakeSTS([response(clock)])
    access, _, sessions, _, sts_options = oidc_access(clock, sts)
    assert not sessions and not sts.calls
    client = access.get_client()
    assert access.get_client() is client
    assert not sts.calls  # Client construction does not exchange a token.
    assert sessions[0].core.get_credentials().method == "custom-modal-web-identity"
    access.authorization_expiry()
    assert len(sts.calls) == 1
    assert sts.calls[0]["RoleArn"] == ROLE
    assert sts.calls[0]["DurationSeconds"] == 3600
    assert sts_options[0]["region_name"] == "eu-west-2"
    config = sts_options[0]["config"]
    assert config.signature_version is UNSIGNED
    assert (config.connect_timeout, config.read_timeout) == (3, 5)


def test_each_refresh_rereads_the_token_and_rotates_credentials_without_mutating_environment():
    clock = Clock()
    sts = FakeSTS([response(clock, lifetime=1200), lambda: response(clock, suffix="two")])
    access, env, sessions, _, _ = oidc_access(clock, sts)
    first = access.presign_get(BUCKET, "match-1.mp4", 3600)
    assert first.actual_ttl_seconds == 1170
    assert first.expires_at == clock() + 1170
    assert first.authorization_expires_at == clock() + 1200
    env["MODAL_IDENTITY_TOKEN"] = "PRIVATE_TEST_OIDC_TWO"
    expected_env = dict(env)
    clock.value += 600
    second = access.presign_get(BUCKET, "match-1.mp4", 3600)
    assert [call["WebIdentityToken"] for call in sts.calls] == ["PRIVATE_TEST_OIDC_ONE", "PRIVATE_TEST_OIDC_TWO"]
    assert sessions[0].clients[-1].kwargs["aws_access_key_id"] == "TEST_ACCESS_two"
    assert second.actual_ttl_seconds == 3570
    assert second.authorization_expires_at == clock() + 3600
    assert env == expected_env
    assert len(sessions[0].clients) == 2
    assert sessions[0].clients[0].closed and not sessions[0].clients[1].closed


def test_head_and_get_are_signed_separately_with_bounded_expiry_and_no_fixed_range():
    clock = Clock()
    access, _, sessions, _, _ = oidc_access(clock, FakeSTS([response(clock, lifetime=1800)]))
    get_result = access.presign_get(BUCKET, "match-2.mp4", 1000)
    head_result = access.presign_head(BUCKET, "match-2.mp4", 2000)
    signer, = sessions[0].clients
    assert signer.calls[0] == ("get_object", {"Params": {"Bucket": BUCKET, "Key": "match-2.mp4"}, "ExpiresIn": 1000})
    assert signer.calls[1][0] == "head_object"
    assert signer.calls[1][1]["ExpiresIn"] == 1770
    assert get_result.expires_at == clock() + 1000
    assert head_result.expires_at == clock() + 1770
    assert not signer.closed
    assert signer.kwargs["config"].signature_version == "s3v4"
    access.close()
    assert signer.closed and signer.close_calls == 1


def test_explicit_token_provider_is_called_for_each_sts_exchange():
    clock = Clock()
    current = ["PRIVATE_PROVIDER_TOKEN_ONE"]
    sts = FakeSTS([response(clock, lifetime=1200), lambda: response(clock, suffix="two")])
    access, _, _, _, _ = oidc_access(clock, sts, {"AWS_ROLE_ARN": ROLE}, token_provider=lambda: current[0])
    access.presign_get(BUCKET, "match-1.mp4")
    current[0] = "PRIVATE_PROVIDER_TOKEN_TWO"
    clock.value += 600
    access.presign_get(BUCKET, "match-1.mp4")
    assert [call["WebIdentityToken"] for call in sts.calls] == ["PRIVATE_PROVIDER_TOKEN_ONE", "PRIVATE_PROVIDER_TOKEN_TWO"]


def test_missing_modal_role_fails_without_falling_back_to_other_credentials():
    access, _, sessions, _, _ = oidc_access(Clock(), FakeSTS([]), {"MODAL_IDENTITY_TOKEN": "PRIVATE_OIDC"})
    with pytest.raises(StorageAuthorizationError, match="aws_web_identity_role_unconfigured"):
        access.get_client()
    assert sessions == []


def test_local_role_setting_without_modal_token_keeps_normal_aws_chain():
    clock = Clock()
    session = FakeSession(Credentials("LOCAL_TEST_ACCESS", "PRIVATE_LOCAL_SECRET"))
    options = []

    def session_factory(**kwargs):
        options.append(kwargs)
        return session

    access = S3Access({"AWS_ROLE_ARN": ROLE, "AWS_PROFILE": "portfolio", "AWS_DEFAULT_REGION": "eu-west-2"},
                      now=clock, session_factory=session_factory,
                      sts_client_factory=lambda **kwargs: pytest.fail("Local mode must not call STS"))
    result = access.presign_get(BUCKET, "match-1.mp4", 3600)
    assert options == [{"region_name": "eu-west-2", "profile_name": "portfolio"}]
    assert result.authorization_expires_at is None
    assert result.actual_ttl_seconds == 3600
    assert session.clients[0].kwargs["aws_access_key_id"] == "LOCAL_TEST_ACCESS"


def test_unknown_local_temporary_credential_expiry_does_not_publish_an_unjustified_deadline():
    session = FakeSession(Credentials("TEST_ACCESS", "PRIVATE_SECRET", "PRIVATE_SESSION"))
    access = S3Access({}, session_factory=lambda **kwargs: session)
    with pytest.raises(StorageAuthorizationError, match="aws_credential_expiry_unknown"):
        access.presign_get(BUCKET, "match-1.mp4")
    assert session.clients == []


def test_generation_and_expiry_are_captured_together_even_after_a_concurrent_refresh():
    clock = Clock()

    class ChangedDuringSnapshot:
        _refresh_lock = Lock()
        _expiry_time = datetime.fromtimestamp(clock() + 1800, timezone.utc)
        _frozen_credentials = ReadOnlyCredentials("LATEST_ACCESS", "LATEST_SECRET", "LATEST_SESSION")

        def get_frozen_credentials(self):
            # Another refresh committed immediately after this older result.
            return ReadOnlyCredentials("OLDER_ACCESS", "OLDER_SECRET", "OLDER_SESSION")

    session = FakeSession(ChangedDuringSnapshot())
    access = S3Access({}, now=clock, session_factory=lambda **kwargs: session)
    result = access.presign_get(BUCKET, "match-1.mp4", 3600)
    assert result.actual_ttl_seconds == 1770
    assert session.clients[0].kwargs["aws_access_key_id"] == "LATEST_ACCESS"


def test_parallel_initial_signing_uses_one_sts_credential_generation():
    clock = Clock()
    sts = FakeSTS([response(clock)])
    access, _, sessions, _, _ = oidc_access(clock, sts)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: access.presign_get(BUCKET, "match-1.mp4"), range(8)))
    assert len(sts.calls) == len(sessions) == 1
    assert len(sessions[0].clients) == 1  # One cached signer for the generation.
    assert len(results) == 8


def test_cached_signer_recomputes_remaining_expiry_without_rebuilding_or_reusing_stale_credentials():
    clock = Clock()
    sts = FakeSTS([response(clock, lifetime=3600)])
    access, _, sessions, _, _ = oidc_access(clock, sts)
    access.warm()
    head_client, signer = sessions[0].clients
    first = access.presign_get(BUCKET, "match-1.mp4", 3600)
    clock.value += 100
    second = access.presign_head(BUCKET, "match-1.mp4", 3600)
    assert first.actual_ttl_seconds == 3570 and second.actual_ttl_seconds == 3470
    assert first.authorization_expires_at == second.authorization_expires_at == clock() + 3500
    assert len(sts.calls) == 1 and sessions[0].clients == [head_client, signer]
    assert [call[0] for call in signer.calls] == ["get_object", "head_object"]
    assert access.close() is True
    assert head_client.close_calls == signer.close_calls == 1
    with pytest.raises(StorageAuthorizationError, match="aws_access_closed"):
        access.presign_get(BUCKET, "match-1.mp4")


def test_credential_snapshot_does_not_hold_owner_lock_while_refresh_callback_can_acquire_it():
    owner = []
    class CheckedCredentials(Credentials):
        def get_frozen_credentials(self):
            assert not owner[0]._lock._is_owned(), "Credential refresh lock ordering inverted"
            return super().get_frozen_credentials()
    session = FakeSession(CheckedCredentials("OFFLINE_ACCESS", "OFFLINE_SECRET"))
    access = S3Access({}, session_factory=lambda **kwargs: session)
    owner.append(access)
    access.warm()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: access.presign_get(BUCKET, "match-1.mp4"), range(8)))
    assert len(results) == 8 and len(session.clients) == 2
    access.close()


def test_sdk_errors_and_url_repr_do_not_expose_tokens(caplog):
    clock = Clock()
    access, _, _, _, _ = oidc_access(clock, FakeSTS([RuntimeError("PRIVATE_TEST_OIDC_LEAK PRIVATE_SECRET")]))
    with pytest.raises(StorageAuthorizationError) as error:
        access.presign_get(BUCKET, "match-1.mp4")
    assert str(error.value) == "aws_web_identity_exchange_failed"
    assert "PRIVATE_TEST_OIDC_LEAK" not in caplog.text
    assert "PRIVATE_SECRET" not in caplog.text
    access, _, _, _, _ = oidc_access(clock, FakeSTS([response(clock)]))
    result = access.presign_get(BUCKET, "match-1.mp4")
    assert "PRIVATE_TEST_SIGNATURE" not in repr(result)
    assert "PRIVATE_TEST_OIDC" not in repr(access)


@pytest.mark.parametrize("expiry", ["not-a-time", datetime(2030, 1, 1), datetime(2000, 1, 1, tzinfo=timezone.utc)])
def test_bad_sts_expirations_are_rejected(expiry):
    clock = Clock()
    value = response(clock)
    value["Credentials"]["Expiration"] = expiry
    access, _, _, _, _ = oidc_access(clock, FakeSTS([value]))
    with pytest.raises(StorageAuthorizationError):
        access.presign_get(BUCKET, "match-1.mp4")


def test_missing_credentials_and_write_operations_fail_before_signing():
    access = S3Access({}, session_factory=lambda **kwargs: FakeSession())
    with pytest.raises(StorageAuthorizationError, match="aws_credentials_unavailable"):
        access.presign_get(BUCKET, "match-1.mp4")
    with pytest.raises(ValueError, match="read operations"):
        access.presign_object(BUCKET, "match-1.mp4", method="put_object")


def test_signing_and_cleanup_failures_do_not_leak_sdk_details():
    class BrokenClient:
        def generate_presigned_url(self, *args, **kwargs):
            raise RuntimeError("PRIVATE_TEST_SOURCE_SIGNATURE")

        def close(self):
            raise RuntimeError("PRIVATE_TEST_CLEANUP_DETAIL")

    session = FakeSession(Credentials("LOCAL_ACCESS", "PRIVATE_LOCAL_SECRET"))
    session.client = lambda *args, **kwargs: BrokenClient()
    access = S3Access({}, session_factory=lambda **kwargs: session)
    with pytest.raises(StorageAuthorizationError) as error:
        access.presign_get(BUCKET, "match-1.mp4")
    assert str(error.value) == "s3_presign_failed"


def test_explicit_client_allows_metadata_reads_and_close_releases_owned_clients():
    clock = Clock()
    sts = FakeSTS([response(clock)])
    access, _, _, _, _ = oidc_access(clock, sts)
    client = access.get_client()
    assert client.head_object(Bucket=BUCKET, Key="match-1.mp4")["ContentLength"] == 1234
    access.authorization_expiry()
    access.close()
    assert client.closed and sts.closed
