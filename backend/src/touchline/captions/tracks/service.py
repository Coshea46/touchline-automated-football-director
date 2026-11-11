"""Read-only, source-isolated access to previously generated caption tracks."""

import json
import os
import re
import time
from pathlib import Path
from threading import Lock

from touchline.captions.tracks.schema import ID_PATTERN, validate_track
from touchline.captions.identity import digest, source_identity, verify_source_identity
from touchline.paths import ARTIFACTS_DIR

DEFAULT_TRACK_DIR = ARTIFACTS_DIR / "captions"


class CaptionService:
    def __init__(self, track_dir=None, sources=None, identities=None, *,
                 identity_checker=verify_source_identity, identity_ttl_seconds=300,
                 now=time.monotonic, wall_now=time.time, source_provider=None):
        self.track_dir = Path(track_dir or os.environ.get("MATCHPULSE_CAPTION_DIR") or DEFAULT_TRACK_DIR)
        if sources is None:
            from touchline.replay.sources import SourceRegistry
            sources = SourceRegistry.from_env().sources
        if isinstance(sources, dict):
            self.sources = {key: value.get("source_key") if isinstance(value, dict) else value
                            for key, value in sources.items()}
            self.source_urls = {key: value.get("source_url") for key, value in sources.items()
                                if isinstance(value, dict)}
            self.source_identity_urls = {key: value.get("source_identity_url") for key, value in sources.items()
                                         if isinstance(value, dict)}
        else:
            sources = tuple(sources)
            self.sources = {source.match_id: source.key for source in sources}
            self.source_urls = {source.match_id: source.source_url for source in sources}
            self.source_identity_urls = {source.match_id: getattr(source, "head_url", None)
                                         for source in sources}
        self.identities = dict(identities or {})
        if source_provider is not None and not callable(source_provider):
            raise TypeError("Caption source provider must be callable")
        self._source_provider = source_provider
        if not 0 <= identity_ttl_seconds <= 300:
            raise ValueError("Caption identity cache must expire within 300 seconds")
        self._checker, self._ttl, self._now = identity_checker, identity_ttl_seconds, now
        self._wall_now = wall_now
        self._cache, self._identity_lock = {}, Lock()

    @classmethod
    def from_metadata(cls, metadata_path, *, track_dir=None, sources=None, **kwargs):
        metadata = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
        records = metadata.get("matches", metadata) if isinstance(metadata, dict) else metadata
        configured = {item["match_id"]: item["source_key"] for item in records}
        catalog_identities = {item["match_id"]: item["source_identity"] for item in records
                              if item.get("source_identity")}
        if sources is None:
            return cls(track_dir, configured, catalog_identities, **kwargs)
        sources = sources if isinstance(sources, dict) else tuple(sources)
        entries = sources.items() if isinstance(sources, dict) else ((item.match_id, item) for item in sources)
        identities = {}
        for match_id, source in entries:
            if isinstance(source, dict):
                key, url = source.get("source_key"), source.get("source_url")
                actual = source.get("identity") or source.get("source_identity")
                bound_metadata = source.get("metadata") or {}
            elif isinstance(source, str):
                key, url, actual, bound_metadata = source, None, None, {}
            else:
                key, url = source.key, source.source_url
                actual, bound_metadata = getattr(source, "identity", None), getattr(source, "metadata", None) or {}
            if key != configured.get(match_id):
                continue
            actual = actual or bound_metadata.get("source_identity")
            if actual:
                identities[match_id] = actual
                continue
            # A catalog digest belongs to its original object locator. Matching
            # a cam ID/key in a new bucket alone must not reuse old captions.
            old = catalog_identities.get(match_id)
            if isinstance(old, dict) and url:
                try:
                    candidate = source_identity(key, old.get("etag"), old.get("content_length"), url)
                except (TypeError, ValueError):
                    continue
                if candidate["digest"] == old.get("digest"):
                    identities[match_id] = old
        return cls(track_dir, sources, identities, **kwargs)

    def _current_identity(self, match_id, configured_key):
        with self._identity_lock:
            cached = self._cache.get(match_id)
            if cached and self._now() < cached[0]:
                return cached[1], cached[0]
            source_url = self.source_urls.get(match_id)
            identity_url = self.source_identity_urls.get(match_id)
            binding_valid = True
            if self._source_provider is not None:
                source = next((item for item in self._source_provider() if item.match_id == match_id), None)
                binding_valid = source is not None and source.key == configured_key
                if binding_valid:
                    source_url, identity_url = source.source_url, getattr(source, "head_url", None)
            if not binding_valid:
                current = None
            elif identity_url is None:
                # Public sources and injected two-argument checkers retain
                # their original interface.
                current = self._checker(configured_key, source_url)
            else:
                current = self._checker(configured_key, source_url, identity_url)
            if isinstance(current, dict):
                current = current.get("digest")
            # Failed HEADs are cached briefly too; never hammer a missing object.
            self._cache[match_id] = (self._now() + (self._ttl if current else min(5, self._ttl)), current)
            return current, self._cache[match_id][0]

    def get(self, match_id, source_key=None, source_identity=None):
        configured_key = self.sources.get(match_id) if isinstance(match_id, str) else None
        result = {"status": "unavailable", "reason": "missing_track", "match_id": match_id,
                  "source_key": configured_key, "source_identity": None, "track_version": None,
                  "language": None, "provenance": None, "timing_accuracy": None,
                  "coverage_intervals": [], "cues": []}
        if not isinstance(match_id, str) or not ID_PATTERN.fullmatch(match_id) or not configured_key:
            return {**result, "reason": "unknown_source"}
        identity = self.identities.get(match_id)
        identity = identity.get("digest") if isinstance(identity, dict) else identity
        result["source_identity"] = identity
        if ((source_key is not None and source_key != configured_key)
                or (source_identity is not None and source_identity != identity)):
            return {**result, "status": "source_mismatch", "reason": "source_mismatch"}
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
            return {**result, "reason": "source_identity_unverified"}
        path = self.track_dir / f"{match_id}-{identity}.json"
        if not path.is_file():
            return result
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("Caption track exceeds size limit")
            track = validate_track(json.loads(path.read_text(encoding="utf-8")))
            if track["match_id"] != match_id or track["source_key"] != configured_key or track["source_identity"] != identity:
                return {**result, "status": "source_mismatch", "reason": "source_mismatch"}
        except (OSError, ValueError, TypeError, KeyError):
            return {**result, "reason": "invalid_track"}
        current, valid_until = self._current_identity(match_id, configured_key)
        if current is None:
            return {**result, "reason": "source_identity_verification_unavailable"}
        if current != identity:
            return {**result, "status": "source_mismatch", "reason": "source_identity_changed"}
        remaining = max(0.0, valid_until - self._now())
        return {**track, "status": "ready", "reason": None, "track_version": digest(track),
                "timing_accuracy": "provider_word",
                "source_identity_revalidate_after_seconds": remaining,
                # The absolute deadline survives the browser's metadata cache.
                # Reusing a response must never renew its source validation.
                "source_identity_valid_until": self._wall_now() + remaining}
