"""Offline, reusable captions with explicit source identity and real timing."""

from touchline.captions.tracks.schema import cue_at, validate_track, webvtt
from touchline.captions.identity import source_identity
from touchline.captions.tracks.service import CaptionService

__all__ = ["CaptionService", "cue_at", "source_identity", "validate_track", "webvtt"]
