"""Refreshable Modal OIDC credentials and expiry-aware S3 read signatures.

AWS verifies the Modal token and role trust policy. This module neither decodes
tokens nor copies credentials into the process environment. SDK imports and
credential resolution happen only when a caller requests AWS access.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from contextlib import contextmanager, suppress
import math
import os
import re
from threading import RLock
import time


class StorageAuthorizationError(RuntimeError):
    """Static error codes only; SDK messages may contain private source data."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PresignedObject:
    url: str = field(repr=False)
    expires_at: float
    authorization_expires_at: float | None
    actual_ttl_seconds: int


def _boto_session(**kwargs):
    import boto3

    return boto3.Session(**kwargs)


def _sts_client(**kwargs):
    import botocore.session

    return botocore.session.get_session().create_client("sts", **kwargs)


def _expiration(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise StorageAuthorizationError("aws_credential_expiry_invalid")
    timestamp = value.timestamp()
    if not math.isfinite(timestamp):
        raise StorageAuthorizationError("aws_credential_expiry_invalid")
    return timestamp


def _credential_snapshot(credentials):
    """Read one credential generation and its matching expiry.

    Botocore 1.31's refresh lock protects both fields. Capture them together after
    asking the provider to refresh, then sign with that immutable generation.
    This also supports the normal local assume-role/SSO credential providers.
    """
    frozen = credentials.get_frozen_credentials()
    refresh_lock = getattr(credentials, "_refresh_lock", None)
    if refresh_lock is not None:
        with refresh_lock:
            frozen = credentials._frozen_credentials
            expiry = getattr(credentials, "_expiry_time", None)
    else:
        expiry = getattr(credentials, "_expiry_time", None)
    return frozen, _expiration(expiry) if expiry is not None else None


class S3Access:
    """Create AWS clients lazily, using OIDC only inside a configured Modal run.

    ``env`` is kept as a live mapping so every STS refresh reads its latest
    MODAL_IDENTITY_TOKEN. An explicit token_provider can supply a rotating token
    source instead. Local callers retain boto3's normal credential chain.
    """

    EXPIRY_MARGIN_SECONDS = 30

    def __init__(self, env=None, *, region=None, now=time.time,
                 session_factory=None, sts_client_factory=None, token_provider=None):
        self._env = os.environ if env is None else env
        self.media_origins = self._env.get("MATCHPULSE_S3_MEDIA_ORIGINS", "single")
        if self.media_origins not in ("single", "split"):
            raise ValueError("invalid_s3_media_origins")
        self.region = region or self._env.get("AWS_REGION") or self._env.get("AWS_DEFAULT_REGION") or "eu-west-2"
        self._now = now
        self._session_factory = session_factory or _boto_session
        self._sts_factory = sts_client_factory or _sts_client
        self._token_provider = token_provider or (lambda: self._env.get("MODAL_IDENTITY_TOKEN"))
        self._explicit_token_provider = token_provider is not None
        self._lock = RLock()
        self._session = self._client = self._sts = None
        self._signers, self._signer_generation = {}, None
        self._references = 0
        self._closing = self._closed = False
        self._close_confirmed = False

    def retain(self):
        """Borrow this app-owned resource before starting any native work."""
        with self._lock:
            if self._closing or self._closed:
                raise StorageAuthorizationError("aws_access_closed")
            self._references += 1
        return self

    def release(self):
        with self._lock:
            if self._references < 1:
                raise RuntimeError("AWS access reference was already released")
            self._references -= 1
            if self._closing and self._references == 0:
                self._close_clients()

    @contextmanager
    def _operation(self):
        with self._lock:
            if self._closed or (self._closing and not self._references):
                raise StorageAuthorizationError("aws_access_closed")
            self._references += 1
        try:
            yield
        finally:
            self.release()

    @property
    def closed(self):
        with self._lock:
            return self._closed

    @staticmethod
    def _config(*, unsigned=False, dualstack=False):
        from botocore import UNSIGNED
        from botocore.config import Config

        return Config(signature_version=UNSIGNED if unsigned else "s3v4",
                      s3={"addressing_style": "virtual"},
                      use_dualstack_endpoint=dualstack,
                      connect_timeout=3, read_timeout=5,
                      retries={"max_attempts": 2})

    def _refresh_oidc(self):
        # Read the current token for every exchange, never a construction-time
        # snapshot. STS owns signature, audience and workspace/subject validation.
        try:
            token = self._token_provider()
            if not isinstance(token, str) or not token:
                raise StorageAuthorizationError("modal_identity_token_unavailable")
            with self._lock:
                if self._sts is None:
                    self._sts = self._sts_factory(region_name=self.region,
                                                  config=self._config(unsigned=True))
                sts = self._sts
            response = sts.assume_role_with_web_identity(
                RoleArn=self._role_arn, RoleSessionName="touchline-video-reader",
                WebIdentityToken=token, DurationSeconds=3600)
            values = response["Credentials"]
            for name in ("AccessKeyId", "SecretAccessKey", "SessionToken"):
                if not isinstance(values.get(name), str) or not values[name]:
                    raise StorageAuthorizationError("aws_credentials_invalid")
            expiry = _expiration(values["Expiration"])
            if expiry <= self._now():
                raise StorageAuthorizationError("aws_credentials_expired")
            return {"access_key": values["AccessKeyId"], "secret_key": values["SecretAccessKey"],
                    "token": values["SessionToken"],
                    "expiry_time": datetime.fromtimestamp(expiry, timezone.utc).isoformat()}
        except StorageAuthorizationError:
            raise
        except Exception:
            raise StorageAuthorizationError("aws_web_identity_exchange_failed") from None

    def _get_session(self):
        with self._lock:
            if self._closed:
                raise StorageAuthorizationError("aws_access_closed")
            if self._session is not None:
                return self._session
            kwargs = {"region_name": self.region}
            use_oidc = bool(self._env.get("MODAL_IDENTITY_TOKEN")) or self._explicit_token_provider
            if use_oidc:
                role = self._env.get("AWS_ROLE_ARN")
                if (not isinstance(role, str) or not re.fullmatch(
                        r"arn:aws(?:-us-gov|-cn)?:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+", role)):
                    raise StorageAuthorizationError("aws_web_identity_role_unconfigured")
                self._role_arn = role
                import botocore.session
                from botocore.credentials import DeferredRefreshableCredentials

                core = botocore.session.get_session()
                core._credentials = DeferredRefreshableCredentials(
                    refresh_using=self._refresh_oidc, method="custom-modal-web-identity",
                    time_fetcher=lambda: datetime.fromtimestamp(self._now(), timezone.utc))
                kwargs["botocore_session"] = core
            elif self._env.get("AWS_PROFILE"):
                kwargs["profile_name"] = self._env["AWS_PROFILE"]
            try:
                self._session = self._session_factory(**kwargs)
            except Exception:
                raise StorageAuthorizationError("aws_session_unavailable") from None
            return self._session

    def get_client(self):
        """Return a reusable S3 client for explicit HEAD/GET operations."""
        with self._lock:
            if self._closed:
                raise StorageAuthorizationError("aws_access_closed")
            if self._client is None:
                try:
                    self._client = self._get_session().client("s3", region_name=self.region,
                                                              endpoint_url=self._s3_endpoint(),
                                                              config=self._config())
                except StorageAuthorizationError:
                    raise
                except Exception:
                    raise StorageAuthorizationError("aws_client_unavailable") from None
            return self._client

    def _snapshot(self):
        try:
            credentials = self._get_session().get_credentials()
            if credentials is None:
                raise StorageAuthorizationError("aws_credentials_unavailable")
            frozen, expiry = _credential_snapshot(credentials)
            if expiry is None and frozen.token:
                # Environment-provided STS credentials may omit their expiry;
                # do not publish a deadline that cannot be justified.
                raise StorageAuthorizationError("aws_credential_expiry_unknown")
            return frozen, expiry
        except StorageAuthorizationError:
            raise
        except Exception:
            raise StorageAuthorizationError("aws_credentials_unavailable") from None

    def _s3_endpoint(self, *, dualstack=False):
        suffix = "amazonaws.com.cn" if self.region.startswith("cn-") else "amazonaws.com"
        kind = "s3.dualstack" if dualstack else "s3"
        return f"https://{kind}.{self.region}.{suffix}"

    def authorization_expiry(self):
        """Resolve/refresh credentials and return their actual expiration, if any."""
        with self._operation():
            return self._snapshot()[1]

    get_authorization_expiry = authorization_expiry

    def _frozen_signer(self, frozen, expiry, *, dualstack=False):
        """Called under owner lock only, after the refreshable snapshot is read."""
        generation = (frozen.access_key, frozen.secret_key, frozen.token, expiry)
        if dualstack not in self._signers or self._signer_generation != generation:
            signer = self._get_session().client(
                "s3", region_name=self.region, endpoint_url=self._s3_endpoint(dualstack=dualstack),
                config=self._config(dualstack=dualstack),
                aws_access_key_id=frozen.access_key, aws_secret_access_key=frozen.secret_key,
                aws_session_token=frozen.token)
            previous = tuple(self._signers.values()) if self._signer_generation != generation else ()
            if previous:
                self._signers = {}
            self._signers[dualstack], self._signer_generation = signer, generation
            for old in previous:
                if old is signer:
                    continue
                with suppress(Exception):
                    old.close()
        return self._signers[dualstack]

    def warm(self):
        """Resolve credentials and build clients without reading/signing an object."""
        with self._operation():
            self.get_client()
            # Never hold the owner lock while Botocore may refresh credentials:
            # its refresh callback acquires this same lock in the other order.
            frozen, expiry = self._snapshot()
            with self._lock:
                self._frozen_signer(frozen, expiry)
                if self.media_origins == "split":
                    self._frozen_signer(frozen, expiry, dualstack=True)
            return expiry

    def presign_object(self, bucket, key, ttl_seconds=3600, *, method="get_object", dualstack=False):
        if type(dualstack) is not bool:
            raise ValueError("invalid_s3_dualstack_mode")
        if method not in ("get_object", "head_object"):
            raise ValueError("Only S3 object read operations can be signed")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 604800:
            raise ValueError("Choose a signature lifetime of 1..604800 seconds")
        if not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise ValueError("Invalid S3 bucket")
        if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 1024:
            raise ValueError("Invalid S3 object key")
        with self._operation():
            frozen, expiry = self._snapshot()
            try:
                with self._lock:
                    # Fixed-generation signer reuse is safe only while this
                    # lock covers signing and replacement/client construction.
                    signing_client = self._frozen_signer(frozen, expiry, dualstack=dualstack)
                    now = self._now()
                    lifetime = ttl_seconds if expiry is None else min(
                        ttl_seconds, math.floor(expiry - now - self.EXPIRY_MARGIN_SECONDS))
                    if lifetime < 1:
                        raise StorageAuthorizationError("aws_credentials_expiring")
                    url = signing_client.generate_presigned_url(
                        method, Params={"Bucket": bucket, "Key": key}, ExpiresIn=lifetime)
                    return PresignedObject(url, now + lifetime, expiry, lifetime)
            except StorageAuthorizationError:
                raise
            except Exception:
                raise StorageAuthorizationError("s3_presign_failed") from None

    def presign_get(self, bucket, key, ttl_seconds=3600, *, dualstack=False):
        return self.presign_object(bucket, key, ttl_seconds, method="get_object", dualstack=dualstack)

    def presign_head(self, bucket, key, ttl_seconds=3600, *, dualstack=False):
        return self.presign_object(bucket, key, ttl_seconds, method="head_object", dualstack=dualstack)

    def _close_clients(self):
        failed, seen = False, set()
        for client in (self._client, self._sts, *self._signers.values()):
            if client is not None and id(client) not in seen:
                seen.add(id(client))
                try:
                    client.close()
                except Exception:
                    failed = True
        self._client = self._sts = self._session = self._signer_generation = None
        self._signers = {}
        self._closed, self._close_confirmed = True, not failed
        if failed:
            raise StorageAuthorizationError("aws_client_close_failed") from None

    def close(self):
        """Request owner shutdown; native borrowers release before SDK close."""
        with self._lock:
            if self._closed:
                return self._close_confirmed
            self._closing = True
            if self._references:
                return False
            self._close_clients()
            return True
