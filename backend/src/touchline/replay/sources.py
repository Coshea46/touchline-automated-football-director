"""Small, bounded source registry; encoded media goes directly to each consumer."""

import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from urllib.parse import quote, urlsplit

from touchline.captions.identity import source_identity, source_locator
from touchline.config.video_sources import (
    LEGACY_PUBLIC_BUCKET, VIDEO_BUCKET, VIDEO_REGION, VIDEO_READER_ROLE, VIDEO_SOURCE_ACCESS,
)
from touchline.match_metadata import is_attested_mirror, metadata_for_source
from touchline.storage import S3Access, StorageAuthorizationError


def number(value, name, *, minimum=0, maximum=float("inf")):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not minimum <= value <= maximum):
        raise ValueError(f"{name} must be a finite number between {minimum} and {maximum}")
    return float(value)


def media_url(value):
    # Never include the supplied value in exceptions: it may contain credentials.
    if not isinstance(value, str):
        raise TypeError("Media source must be an HTTPS URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or len(value) > 16384):
        raise ValueError("Media source must be an HTTPS URL without embedded credentials")
    return value


@dataclass(frozen=True)
class MatchSource:
    match_id: str
    label: str
    offset_seconds: float = 0.0
    key: str | None = None
    source_url: str | None = field(default=None, repr=False)
    playback_url: str | None = field(default=None, repr=False)
    expires_at: float | None = None
    metadata: dict | None = field(default=None, repr=False, compare=False)
    head_url: str | None = field(default=None, repr=False, compare=False)
    identity: dict | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.match_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.match_id):
            raise ValueError("Invalid match ID")
        if not isinstance(self.label, str) or not 1 <= len(self.label) <= 100:
            raise ValueError("Match label must contain 1 to 100 characters")
        number(self.offset_seconds, "offset_seconds", maximum=86400)
        if self.key is not None and (not isinstance(self.key, str) or not self.key
                                     or len(self.key) > 1024 or self.key.startswith("/")):
            raise ValueError("Invalid S3 object key")
        if self.source_url is not None:
            media_url(self.source_url)
        if self.playback_url is not None:
            media_url(self.playback_url)
        if self.head_url is not None:
            media_url(self.head_url)
        if self.expires_at is not None:
            number(self.expires_at, "expires_at")
        if self.key is None and self.source_url is None:
            raise ValueError("Each match requires an S3 key or authorised source URL")

    def worker_spec(self):
        if self.source_url is None or self.playback_url is None:
            raise ValueError("Source URLs have not been resolved")
        return {"match_id": self.match_id, "label": self.label,
                "source_url": self.source_url, "playback_url": self.playback_url,
                "offset_seconds": self.offset_seconds}

    def public_spec(self, healthy=True):
        result = {"match_id": self.match_id, "label": self.label, "url": self.playback_url,
                  "offset_seconds": self.offset_seconds, "healthy": bool(healthy), "source_key": self.key}
        if self.expires_at is not None:
            result["url_expires_at"] = self.expires_at
        identity = self.identity or (self.metadata or {}).get("source_identity")
        if identity:
            result["source_identity"] = identity.get("digest")
        if self.metadata:
            result["metadata"] = self.metadata
            result["fixture_verified"] = True
        return result


class SourceRegistry:
    """Configured sources only; WebSocket clients cannot choose worker URLs.

    The existing public demo bucket works without AWS credentials. Private buckets
    use ``MATCHPULSE_SOURCE_ACCESS=presigned`` and the normal AWS credential chain.
    A refreshed URL doesn't alter match identity or the source-time offset.
    """

    def __init__(self, sources, *, bucket=LEGACY_PUBLIC_BUCKET, region=VIDEO_REGION,
                 access="public", url_ttl_seconds=3600, s3_client=None, s3_access=None,
                 aws_env=None, now=time.time, owns_access=True, media_origins="single"):
        self.sources = tuple(sources)
        if not 1 <= len(self.sources) <= 7:
            raise ValueError("Configure between one and seven matches")
        if len({source.match_id for source in self.sources}) != len(self.sources):
            raise ValueError("Match IDs must be unique")
        if not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise ValueError("Invalid S3 bucket name")
        if not isinstance(region, str) or not re.fullmatch(r"[a-z0-9-]+", region):
            raise ValueError("Invalid AWS region")
        if access not in ("public", "presigned"):
            raise ValueError("MATCHPULSE_SOURCE_ACCESS must be public or presigned")
        if media_origins not in ("single", "split"):
            raise ValueError("invalid_s3_media_origins")
        if media_origins == "split" and s3_client is not None:
            raise ValueError("split_media_requires_expiry_aware_access")
        if type(owns_access) is not bool or (not owns_access and s3_access is None):
            raise ValueError("Borrowed source access requires an explicit owner")
        number(url_ttl_seconds, "url_ttl_seconds", minimum=120, maximum=21600)
        self.bucket, self.region, self.access = bucket, region, access
        self.media_origins = media_origins
        self.url_ttl_seconds, self._s3, self.now = int(url_ttl_seconds), s3_client, now
        self._access = s3_access
        self._owns_access, self._access_reference = owns_access, False
        if s3_access is not None and not owns_access:
            if s3_access.region != self.region:
                raise ValueError("Shared source access region must match the registry")
            s3_access.retain()
            self._access_reference = True
        credential_env = os.environ if aws_env is None else aws_env
        self._aws_env = dict(credential_env)
        self._token_provider = ((lambda: credential_env.get("MODAL_IDENTITY_TOKEN"))
                                if credential_env.get("MODAL_IDENTITY_TOKEN") else None)
        if self.bucket == VIDEO_BUCKET:
            self._aws_env.setdefault("AWS_ROLE_ARN", VIDEO_READER_ROLE)
        self._aws_env.setdefault("AWS_DEFAULT_REGION", self.region)
        self._resolved = ()
        self._identity_pins = {}
        self.resolve_metrics = {}

    @classmethod
    def from_env(cls, env=None, *, s3_access=None, owns_access=True):
        env = os.environ if env is None else env
        bucket = env.get("S3_BUCKET_NAME") or VIDEO_BUCKET
        raw = env.get("MATCHPULSE_SOURCES_JSON")
        if raw:
            try:
                records = json.loads(raw)
                if not isinstance(records, list):
                    raise TypeError
                sources = [MatchSource(**record) for record in records]
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError("Invalid MATCHPULSE_SOURCES_JSON source configuration") from exc
        else:
            count = int(env.get("MATCHPULSE_MATCH_COUNT", "7"))
            if not 1 <= count <= 7:
                raise ValueError("MATCHPULSE_MATCH_COUNT must be between 1 and 7")
            try:
                offsets = json.loads(env.get("MATCHPULSE_OFFSETS_JSON") or "{}")
                if not isinstance(offsets, dict):
                    raise TypeError
                if set(offsets) - {f"cam-{i}" for i in range(1, count + 1)}:
                    raise ValueError
                sources = []
                for i in range(1, count + 1):
                    match_id, key = f"cam-{i}", f"match-{i}.mp4"
                    metadata = metadata_for_source(match_id, key, bucket)
                    sources.append(MatchSource(match_id, metadata["display_name"] if metadata else f"Match {i}",
                                               offsets.get(match_id, 0), key=key, metadata=metadata))
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid MATCHPULSE_OFFSETS_JSON") from exc
        return cls(sources, bucket=bucket,
                   region=env.get("AWS_DEFAULT_REGION") or env.get("AWS_REGION") or VIDEO_REGION,
                   access=env.get("MATCHPULSE_SOURCE_ACCESS") or VIDEO_SOURCE_ACCESS,
                   url_ttl_seconds=int(env.get("MATCHPULSE_SOURCE_URL_TTL", "3600")), aws_env=env,
                   s3_access=s3_access, owns_access=owns_access,
                   media_origins=env.get("MATCHPULSE_S3_MEDIA_ORIGINS", "single"))

    def resolve(self):
        started = time.perf_counter()
        metrics = {"source_count": len(self.sources), "scheduled_head_count": 0, "head_workers": 0,
                   "client_setup_seconds": 0.0, "head_wall_seconds": 0.0,
                   "presign_seconds": 0.0, "succeeded": False}
        try:
            result = self._resolve(metrics)
            metrics["succeeded"] = True
            return result
        finally:
            metrics["elapsed_seconds"] = time.perf_counter() - started
            # Counts/durations only, including attempts that fail authorization.
            self.resolve_metrics = metrics

    def _private_heads(self, metrics):
        sources = tuple(source for source in self.sources if source.key is not None)
        if self.access != "presigned" or not sources:
            return {}
        try:
            started = time.perf_counter()
            try:
                # Construct one shared low-level S3 client on its owning thread.
                # Only read-only HEAD runs concurrently; signing stays serial.
                if self._access is None and self._s3 is None:
                    self._access = S3Access(env=self._aws_env, region=self.region,
                                            now=self.now, token_provider=self._token_provider)
                client = self._s3 if self._s3 is not None else self._access.get_client()
            finally:
                metrics["client_setup_seconds"] = time.perf_counter() - started
            metrics.update(head_workers=min(4, len(sources)), scheduled_head_count=len(sources))
            started = time.perf_counter()
            try:
                with ThreadPoolExecutor(max_workers=metrics["head_workers"], thread_name_prefix="source-head") as pool:
                    futures = [(source.match_id, pool.submit(client.head_object,
                                Bucket=self.bucket, Key=source.key)) for source in sources]
                    try:
                        return {match_id: future.result() for match_id, future in futures}
                    except BaseException:
                        for _, future in futures:
                            future.cancel()
                        # Join running HEADs before resolve can finish and its
                        # owner can close the reusable AWS client.
                        raise
            finally:
                metrics["head_wall_seconds"] = time.perf_counter() - started
        except StorageAuthorizationError:
            raise
        except Exception:
            raise StorageAuthorizationError("source_access_unavailable") from None

    def _resolve(self, metrics):
        heads = self._private_heads(metrics)
        resolved = []
        pins = dict(self._identity_pins)
        private_index = 0
        for source in self.sources:
            # Internal IDs alone never identify a custom source as one of the
            # historical demo fixtures. Match the actual bucket/object binding.
            source = replace(source, metadata=metadata_for_source(source.match_id, source.key, self.bucket))
            if source.key is None:
                resolved.append(replace(source, playback_url=source.playback_url or source.source_url))
                continue
            if self.access == "presigned":
                try:
                    head = heads[source.match_id]
                    started = time.perf_counter()
                    try:
                        if self._access is not None:
                            get = self._access.presign_get(self.bucket, source.key, self.url_ttl_seconds)
                            probe = self._access.presign_head(self.bucket, source.key, self.url_ttl_seconds)
                            url, head_url = get.url, probe.url
                            expires_at = min(get.expires_at, probe.expires_at)
                            playback_url = url
                            if self.media_origins == "split" and private_index % 2:
                                alternate = self._access.presign_get(self.bucket, source.key,
                                    self.url_ttl_seconds, dualstack=True)
                                parsed = urlsplit(media_url(alternate.url))
                                suffix = "amazonaws.com.cn" if self.region.startswith("cn-") else "amazonaws.com"
                                expected_host = f"{self.bucket}.s3.dualstack.{self.region}.{suffix}"
                                if (parsed.netloc != expected_host
                                        or parsed.path != "/" + quote(source.key, safe="/")
                                        or not parsed.query):
                                    raise StorageAuthorizationError("source_playback_locator_mismatch")
                                playback_url = alternate.url
                                expires_at = min(expires_at, alternate.expires_at)
                        else:
                            # Explicit injected clients are deterministic adapters;
                            # production uses the expiry-aware access layer.
                            params = {"Bucket": self.bucket, "Key": source.key}
                            url = self._s3.generate_presigned_url("get_object", Params=params, ExpiresIn=self.url_ttl_seconds)
                            head_url = self._s3.generate_presigned_url("head_object", Params=params, ExpiresIn=self.url_ttl_seconds)
                            expires_at = self.now() + self.url_ttl_seconds
                            playback_url = url
                    finally:
                        metrics["presign_seconds"] += time.perf_counter() - started
                    identity = source_identity(source.key, head.get("ETag"), head.get("ContentLength"), url)
                    if source_locator(head_url) != source_locator(url):
                        raise StorageAuthorizationError("source_authorization_locator_mismatch")
                    old = pins.get(source.match_id)
                    if old is not None and old != identity["digest"]:
                        raise StorageAuthorizationError("source_object_changed")
                    metadata = metadata_for_source(source.match_id, source.key, self.bucket,
                                                   verified_identity=identity, region=self.region)
                    if is_attested_mirror(self.bucket, self.region) and metadata is None:
                        raise StorageAuthorizationError("source_copy_metadata_mismatch")
                    pins[source.match_id] = identity["digest"]
                except StorageAuthorizationError:
                    raise
                except Exception:
                    # Credential, SDK and URL errors must not disclose signed queries.
                    raise StorageAuthorizationError("source_access_unavailable") from None
                label = metadata["display_name"] if metadata else source.label
                resolved.append(replace(source, source_url=url, playback_url=playback_url, head_url=head_url,
                                        expires_at=expires_at, identity=identity, metadata=metadata, label=label))
                private_index += 1
                continue
            else:
                url = f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{quote(source.key, safe='/')}"
                expires_at = None
            media_url(url)
            resolved.append(replace(source, source_url=url, playback_url=url, expires_at=expires_at))
        self._resolved = tuple(resolved)
        self._identity_pins = pins
        return self._resolved

    def needs_refresh(self):
        return any(source.key is not None and source.expires_at is not None and source.expires_at - self.now() <= 60
                   for source in self._resolved)

    def close(self):
        if self._access_reference:
            self._access_reference = False
            self._access.release()
        elif self._access is not None and self._owns_access:
            self._access.close()
