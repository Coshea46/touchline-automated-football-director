"""Small, bounded source registry; encoded media goes directly to each consumer."""

import json
import math
import os
import re
import time
from dataclasses import dataclass, field, replace
from urllib.parse import quote, urlsplit

from touchline.match_metadata import metadata_for_source


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
        if self.metadata:
            result["metadata"] = self.metadata
            result["source_identity"] = self.metadata.get("source_identity", {}).get("digest")
        return result


class SourceRegistry:
    """Configured sources only; WebSocket clients cannot choose worker URLs.

    The existing public demo bucket works without AWS credentials. Private buckets
    use ``MATCHPULSE_SOURCE_ACCESS=presigned`` and the normal AWS credential chain.
    A refreshed URL doesn't alter match identity or the source-time offset.
    """

    def __init__(self, sources, *, bucket="match-switcher-poc-videos", region="eu-west-2",
                 access="public", url_ttl_seconds=3600, s3_client=None, now=time.time):
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
        number(url_ttl_seconds, "url_ttl_seconds", minimum=120, maximum=21600)
        self.bucket, self.region, self.access = bucket, region, access
        self.url_ttl_seconds, self._s3, self.now = int(url_ttl_seconds), s3_client, now
        self._resolved = ()

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
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
                    metadata = metadata_for_source(match_id, key, env.get("S3_BUCKET_NAME") or "match-switcher-poc-videos")
                    sources.append(MatchSource(match_id, metadata["display_name"] if metadata else f"Match {i}",
                                               offsets.get(match_id, 0), key=key, metadata=metadata))
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid MATCHPULSE_OFFSETS_JSON") from exc
        return cls(sources, bucket=env.get("S3_BUCKET_NAME") or "match-switcher-poc-videos",
                   region=env.get("AWS_DEFAULT_REGION") or "eu-west-2",
                   access=env.get("MATCHPULSE_SOURCE_ACCESS", "public"),
                   url_ttl_seconds=int(env.get("MATCHPULSE_SOURCE_URL_TTL", "3600")))

    def resolve(self):
        now = self.now()
        resolved = []
        for source in self.sources:
            # Internal IDs alone never identify a custom source as one of the
            # historical demo fixtures. Match the actual bucket/object binding.
            source = replace(source, metadata=metadata_for_source(source.match_id, source.key, self.bucket))
            if source.key is None:
                resolved.append(replace(source, playback_url=source.playback_url or source.source_url))
                continue
            if self.access == "presigned":
                if self._s3 is None:
                    import boto3
                    from botocore.config import Config

                    self._s3 = boto3.client("s3", region_name=self.region,
                                           config=Config(signature_version="s3v4",
                                                         connect_timeout=3, read_timeout=5,
                                                         retries={"max_attempts": 2}))
                url = self._s3.generate_presigned_url("get_object", Params={
                    "Bucket": self.bucket, "Key": source.key}, ExpiresIn=self.url_ttl_seconds)
                expires_at = now + self.url_ttl_seconds
            else:
                url = f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{quote(source.key, safe='/')}"
                expires_at = None
            media_url(url)
            resolved.append(replace(source, source_url=url, playback_url=url, expires_at=expires_at))
        self._resolved = tuple(resolved)
        return self._resolved

    def needs_refresh(self):
        return any(source.key is not None and source.expires_at is not None and source.expires_at - self.now() <= 60
                   for source in self._resolved)
