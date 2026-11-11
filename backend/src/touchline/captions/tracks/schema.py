"""Source-bound caption tracks; all cue times use the original media clock."""

import math
import re

from touchline.captions.identity import canonical_json, digest, source_identity

SCHEMA_VERSION = 2
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def finite_time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Caption times must be finite nonnegative numbers")
    return float(value)


def validate_track(track):
    if not isinstance(track, dict) or track.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported caption track schema")
    if not isinstance(track.get("match_id"), str) or not ID_PATTERN.fullmatch(track["match_id"]):
        raise ValueError("Invalid caption match ID")
    if not isinstance(track.get("source_key"), str) or not track["source_key"]:
        raise ValueError("Caption source key is required")
    if not re.fullmatch(r"[a-f0-9]{64}", str(track.get("source_identity", ""))):
        raise ValueError("Verified caption source identity is required")
    if not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", str(track.get("language", ""))):
        raise ValueError("Invalid caption language")
    provenance = track.get("provenance", {})
    if (not isinstance(provenance, dict) or provenance.get("provider") != "elevenlabs" or provenance.get("model") != "scribe_v2"
            or provenance.get("timestamp_method") != "provider_word"
            or provenance.get("machine_generated") is not True
            or not isinstance(provenance.get("reviewed"), bool)):
        raise ValueError("Unsupported or missing caption timing provenance")
    intervals = track.get("coverage_intervals")
    if not isinstance(intervals, list) or not 1 <= len(intervals) <= 1000:
        raise ValueError("Caption coverage is required")
    previous = -1.0
    for interval in intervals:
        if not isinstance(interval, dict):
            raise TypeError("Invalid caption coverage interval")
        a, b = finite_time(interval["start_seconds"]), finite_time(interval["end_seconds"])
        if b <= a or a < previous:
            raise ValueError("Invalid or overlapping caption coverage")
        previous = b
    cues = track.get("cues")
    if not isinstance(cues, list) or len(cues) > 10000:
        raise ValueError("Invalid caption cue count")
    previous, ids = -1.0, set()
    for cue in cues:
        if not isinstance(cue, dict):
            raise TypeError("Invalid caption cue")
        a, b = finite_time(cue["start_seconds"]), finite_time(cue["end_seconds"])
        if b <= a or a < previous:
            raise ValueError("Caption cues must be ordered by source start time")
        if not any(interval["start_seconds"] <= a < b <= interval["end_seconds"] for interval in intervals):
            raise ValueError("Caption cue extends beyond verified coverage")
        if (not isinstance(cue.get("id"), str) or not cue["id"] or cue["id"] in ids
                or not isinstance(cue.get("text"), str) or not cue["text"].strip()
                or len(cue["text"]) > 4000 or "\x00" in cue["text"]):
            raise ValueError("Invalid caption cue")
        if cue.get("language", track["language"]) != track["language"]:
            raise ValueError("Cue language disagrees with its track")
        words = cue.get("words")
        if not isinstance(words, list) or not words:
            raise ValueError("Caption cue requires actual provider words or events")
        previous_word = -1.0
        for word in words:
            x, y = finite_time(word["start_seconds"]), finite_time(word["end_seconds"])
            if (not a <= x <= y <= b or x < previous_word
                    or word.get("type") not in ("word", "audio_event")
                    or not isinstance(word.get("text"), str) or not word["text"].strip()):
                raise ValueError("Caption word timestamps are invalid")
            previous_word = x
        if a != min(word["start_seconds"] for word in words) or b != max(word["end_seconds"] for word in words):
            raise ValueError("Cue timing must use actual provider word boundaries")
        previous = a
        ids.add(cue["id"])
    canonical_json(track)
    return track


def cue_at(track, source_time):
    """Half-open boundaries ensure pause/seek/switch never reuse an expired cue."""
    source_time = finite_time(source_time)
    return next((cue for cue in track["cues"]
                 if cue["start_seconds"] <= source_time < cue["end_seconds"]), None)


def webvtt(track):
    validate_track(track)

    def stamp(seconds):
        ms = round(seconds * 1000)
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02}.{ms % 1000:03}"

    rows = ["WEBVTT", "", "NOTE ElevenLabs Scribe v2; machine-generated provider word timing; not human reviewed.", ""]
    for cue in track["cues"]:
        # VTT text must not accidentally become cue markup or a timing arrow.
        text = cue["text"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        rows.extend([cue["id"], f"{stamp(cue['start_seconds'])} --> {stamp(cue['end_seconds'])}",
                     " ".join(text.split()), ""])
    return "\n".join(rows)
