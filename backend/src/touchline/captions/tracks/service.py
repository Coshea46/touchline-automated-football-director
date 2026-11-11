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
                 now=time.monotonic, wall_now=time.time):
        self.track_dir = Path(track_dir or os.environ.get("MATCHPULSE_CAPTION_DIR") or DEFAULT_TRACK_DIR)
        if sources is None:
            from touchline.replay.sources import SourceRegistry
            sources = SourceRegistry.from_env().sources
        if isinstance(sources, dict):
            self.sources = {key: value.get("source_key") if isinstance(value, dict) else value
                            for key, value in sources.items()}
            self.source_urls = {key: value.get("source_url") for key, value in sources.items()
                                if isinstance(value, dict)}
        else:
            self.sources = {source.match_id: source.key for source in sources}
            self.source_urls = {source.match_id: source.source_url for source in sources}
        self.identities = dict(identities or {})
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
        identities = {item["match_id"]: item["source_identity"] for item in records
                      if item.get("source_identity")}
        return cls(track_dir, sources if sources is not None else configured, identities, **kwargs)

    def _current_identity(self, match_id, configured_key):
        with self._identity_lock:
            cached = self._cache.get(match_id)
            if cached and self._now() < cached[0]:
                return cached[1], cached[0]
            current = self._checker(configured_key, self.source_urls.get(match_id))
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
