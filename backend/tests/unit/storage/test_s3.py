from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock

from botocore import UNSIGNED
from botocore.credentials import Credentials, ReadOnlyCredentials
import pytest

from touchline.storage import S3Access, StorageAuthorizationError


ROLE = "arn:aws:iam::709860105019:role/TouchlineVideoReader"
BUCKET = "touchline-videos-709860105019-eu-west-2-an"


class Clock:
    def __init__(self):
        self.value = 1_800_000_000.0

    def __call__(self):
        return self.value


def response(clock, *, suffix="one", lifetime=3600):
    return {"Credentials": {
        "AccessKeyId": "TEST_ACCESS_" + suffix,
        "SecretAccessKey": "PRIVATE_TEST_SECRET_" + suffix,
        "SessionToken": "PRIVATE_TEST_SESSION_" + suffix,
        "Expiration": datetime.fromtimestamp(clock() + lifetime, timezone.utc),
    }}


class FakeSTS:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def assume_role_with_web_identity(self, **kwargs):
        self.calls.append(kwargs)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value() if callable(value) else value

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, session, kwargs):
        self.session, self.kwargs = session, kwargs
        self.calls = []
        self.closed = False

    def generate_presigned_url(self, method, **kwargs):
        self.calls.append((method, kwargs))
        return "https://private.example/video?signature=PRIVATE_TEST_SIGNATURE"

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        return {"ContentLength": 1234, "ETag": '"test-etag"'}

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, credentials=None, *, botocore_session=None):
        self.credentials = credentials
        self.core = botocore_session
        self.clients = []

    def get_credentials(self):
        return self.core.get_credentials() if self.core is not None else self.credentials

    def client(self, name, **kwargs):
        assert name == "s3"
        client = FakeClient(self, kwargs)
        self.clients.append(client)
        return client


def oidc_access(clock, sts, env=None, **options):
    env = env if env is not None else {"MODAL_IDENTITY_TOKEN": "PRIVATE_TEST_OIDC_ONE", "AWS_ROLE_ARN": ROLE}
    sessions, session_options, sts_options = [], [], []

    def session_factory(**kwargs):
        session_options.append(kwargs)
        session = FakeSession(botocore_session=kwargs.get("botocore_session"))
        sessions.append(session)
        return session

    def sts_factory(**kwargs):
        sts_options.append(kwargs)
        return sts

    access = S3Access(env, now=clock, session_factory=session_factory,
                      sts_client_factory=sts_factory, **options)
    return access, env, sessions, session_options, sts_options


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


def test_head_and_get_are_signed_separately_with_bounded_expiry_and_no_fixed_range():
    clock = Clock()
    access, _, sessions, _, _ = oidc_access(clock, FakeSTS([response(clock, lifetime=1800)]))
    get_result = access.presign_get(BUCKET, "match-2.mp4", 1000)
    head_result = access.presign_head(BUCKET, "match-2.mp4", 2000)
    get_client, head_client = sessions[0].clients
    assert get_client.calls == [("get_object", {"Params": {"Bucket": BUCKET, "Key": "match-2.mp4"}, "ExpiresIn": 1000})]
    assert head_client.calls[0][0] == "head_object"
    assert head_client.calls[0][1]["ExpiresIn"] == 1770
    assert get_result.expires_at == clock() + 1000
    assert head_result.expires_at == clock() + 1770
    assert get_client.closed and head_client.closed
    assert get_client.kwargs["config"].signature_version == "s3v4"


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
    assert len(results) == 8


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
