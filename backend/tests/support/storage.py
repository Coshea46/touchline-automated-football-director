"""Synthetic AWS credential/client fixtures; no HTTP requests are performed."""

from datetime import datetime, timezone

from touchline.storage import S3Access

ROLE = "arn:aws:iam::709860105019:role/TouchlineVideoReader"
BUCKET = "touchline-videos-709860105019-eu-west-2-an"


class Clock:
    def __init__(self):
        self.value = 1_800_000_000.0

    def __call__(self):
        return self.value


def response(clock, *, suffix="one", lifetime=3600):
    return {"Credentials": {"AccessKeyId": "TEST_ACCESS_" + suffix,
        "SecretAccessKey": "PRIVATE_TEST_SECRET_" + suffix,
        "SessionToken": "PRIVATE_TEST_SESSION_" + suffix,
        "Expiration": datetime.fromtimestamp(clock() + lifetime, timezone.utc)}}


class FakeSTS:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
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
        self.closed, self.close_calls = False, 0

    def generate_presigned_url(self, method, **kwargs):
        self.calls.append((method, kwargs))
        return "https://private.example/video?signature=PRIVATE_TEST_SIGNATURE"

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        return {"ContentLength": 1234, "ETag": '"test-etag"'}

    def close(self):
        self.closed = True
        self.close_calls += 1


class FakeSession:
    def __init__(self, credentials=None, *, botocore_session=None):
        self.credentials, self.core = credentials, botocore_session
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
