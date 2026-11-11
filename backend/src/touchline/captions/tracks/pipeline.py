"""Idempotent, explicitly bounded offline caption generation."""

import json
import math
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from touchline.captions.tracks.audio import decode_audio
from touchline.captions.tracks.provider import MODEL, PRICING_CHECKED, SDK_VERSION, ElevenLabsTranscriber, estimate_cost
from touchline.captions.errors import CaptionError
from touchline.captions.tracks.schema import SCHEMA_VERSION, finite_time, validate_track, webvtt
from touchline.captions.identity import canonical_json, digest, source_identity
from touchline.captions.tracks.service import DEFAULT_TRACK_DIR


def atomic_write(path, text):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def ledger_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".generation.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise CaptionError("caption_generation_busy") from None
    try:
        os.close(descriptor)
        yield
    finally:
        lock.unlink(missing_ok=True)


def words_to_track(response, *, match_id, source_key, identity, start, end, language, audio_metadata):
    if not isinstance(response, dict) or not isinstance(response.get("words"), list):
        raise CaptionError("provider_word_timestamps_missing")
    if len(response["words"]) > 10000:
        raise CaptionError("provider_word_count_limit")
    detected = response.get("language_code")
    track_language = detected if language == "auto" else language
    cues, group, tokens, previous = [], [], [], -1.0
    job_prefix = digest([start, end, language])[:12]

    def flush():
        if not group:
            return
        a = min(word["start_seconds"] for word in group)
        b = max(word["end_seconds"] for word in group)
        if b <= a:
            raise CaptionError("provider_word_timestamps_missing")
        cues.append({"id": f"{job_prefix}-{len(cues)}", "start_seconds": a, "end_seconds": b,
                     "text": " ".join(word["text"].strip() for word in group),
                     "language": track_language, "kind": "audio_event" if group[0]["type"] == "audio_event" else "speech",
                     "speaker_id": group[0].get("speaker_id"), "words": list(group)})
        group.clear()

    for token in response["words"]:
        if not isinstance(token, dict):
            raise CaptionError("provider_word_invalid")
        if token.get("type") == "spacing":
            continue
        try:
            a, b = finite_time(token["start"]), finite_time(token["end"])
            text, kind, speaker = token["text"], token["type"], token.get("speaker_id")
        except (ValueError, KeyError, TypeError):
            raise CaptionError("provider_word_invalid") from None
        if (b < a or b > end - start or a < previous or kind not in ("word", "audio_event")
                or not isinstance(text, str) or not text.strip() or len(text) > 4000
                or speaker is not None and (not isinstance(speaker, str) or len(speaker) > 128)):
            raise CaptionError("provider_word_invalid")
        previous = a
        word = {"text": text, "type": kind, "start_seconds": start + a, "end_seconds": start + b,
                "speaker_id": speaker}
        if isinstance(token.get("logprob"), (int, float)) and math.isfinite(token["logprob"]):
            word["logprob"] = token["logprob"]
        tokens.append(word)
        if group and (kind == "audio_event" or group[0]["type"] == "audio_event"
                      or group[0].get("speaker_id") != speaker or a + start - group[-1]["end_seconds"] > .7
                      or b + start - group[0]["start_seconds"] > 5 or len(group) >= 14
                      or sum(len(item["text"]) + 1 for item in group) + len(text) > 84):
            flush()
        group.append(word)
        if kind == "audio_event" or (len(group) >= 3 and text.rstrip().endswith((".", "?", "!"))):
            flush()
    flush()
    if not tokens and str(response.get("text", "")).strip():
        raise CaptionError("provider_word_timestamps_missing")
    cues.sort(key=lambda cue: cue["start_seconds"])
    track = {"schema_version": SCHEMA_VERSION, "match_id": match_id, "source_key": source_key,
             "source_identity": identity, "language": track_language,
             "coverage_intervals": [{"start_seconds": start, "end_seconds": end}], "cues": cues,
             "provenance": {"provider": "elevenlabs", "model": MODEL, "machine_generated": True,
                            "timestamp_method": "provider_word", "reviewed": False,
                            "sdk_version": SDK_VERSION, "requested_language": language,
                            "grouping": "word_boundaries_pause_speaker_punctuation_5s_14words_84chars",
                            "created_at": datetime.now(UTC).isoformat(),
                            "audio_windows": [{"start_seconds": start, "end_seconds": end, **audio_metadata}]}}
    try:
        return validate_track(track)
    except (ValueError, TypeError, KeyError):
        raise CaptionError("provider_word_invalid") from None


class CaptionPipeline:
    def __init__(self, track_dir=None, *, transcriber=None, decoder=decode_audio):
        self.directory = Path(track_dir or DEFAULT_TRACK_DIR)
        self.transcriber = transcriber
        self.decoder = decoder

    def generate(self, *, match_id, source_key, identity, source_url, start_seconds,
                 end_seconds, model=MODEL, language="en", max_seconds=30,
                 max_cost_usd=.003, execute=False):
        start, end = finite_time(start_seconds), finite_time(end_seconds)
        duration = end - start
        if not 0 < duration <= max_seconds <= 30:
            raise CaptionError("duration_budget_exceeded")
        if abs(start * 16000 - round(start * 16000)) > 1e-5 or abs(end * 16000 - round(end * 16000)) > 1e-5:
            raise CaptionError("audio_sample_boundary_required")
        if not isinstance(identity, dict) or not identity.get("digest"):
            raise CaptionError("source_identity_unverified")
        try:
            expected_identity = source_identity(source_key, identity["etag"], identity["content_length"], source_url)
        except (KeyError, TypeError, ValueError):
            raise CaptionError("source_identity_unverified") from None
        if expected_identity != identity:
            raise CaptionError("source_identity_mismatch")
        cost = estimate_cost(duration, model)
        if cost > finite_time(max_cost_usd) + 1e-12:
            raise CaptionError("cost_budget_exceeded")
        plan = {"match_id": match_id, "source_key": source_key, "source_identity": identity["digest"],
                "model": model, "language": language, "start_seconds": start, "end_seconds": end,
                "estimated_cost_usd": cost, "pricing_checked": PRICING_CHECKED,
                "timestamp_method": "provider_word"}
        job = digest({key: value for key, value in plan.items() if key not in ("estimated_cost_usd", "pricing_checked")})
        if not execute:
            return {**plan, "status": "estimate", "job_id": job,
                    "caption_support": "native_word_timestamps"}
        filename = f"{match_id}-{identity['digest']}"
        # Validate the public identity/path fields BEFORE touching disk or upload.
        words_to_track({"words": [], "text": "", "language_code": "und"}, match_id=match_id, source_key=source_key,
                          identity=identity["digest"], start=start, end=end,
                          language=language, audio_metadata={})
        with ledger_lock(self.directory):
            ledger_path = self.directory / "ledger.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {"jobs": {}}
            jobs = ledger["jobs"]
            if job in jobs:
                if jobs[job]["status"] == "complete":
                    generated = self.directory / f"{filename}.json"
                    if not generated.is_file():
                        raise CaptionError("completed_track_missing_review_required")
                    try:
                        completed = validate_track(json.loads(generated.read_text(encoding="utf-8")))
                    except (OSError, ValueError, TypeError, KeyError):
                        raise CaptionError("completed_track_invalid_review_required") from None
                    if (completed["match_id"] != match_id or completed["source_key"] != source_key
                            or completed["source_identity"] != identity["digest"]
                            or completed["provenance"].get("requested_language") != language
                            or language != "auto" and completed["language"] != language
                            or completed["provenance"]["model"] != model):
                        raise CaptionError("completed_track_identity_mismatch")
                    covered_until = start
                    for interval in completed["coverage_intervals"]:
                        if interval["start_seconds"] > covered_until:
                            break
                        covered_until = max(covered_until, interval["end_seconds"])
                    if covered_until < end:
                        raise CaptionError("completed_track_coverage_mismatch")
                    return {**plan, "status": "reused", "job_id": job}
                raise CaptionError("job_already_reserved_review_required")
            existing_path = self.directory / f"{filename}.json"
            existing = validate_track(json.loads(existing_path.read_text(encoding="utf-8"))) if existing_path.exists() else None
            if existing:
                if (existing["source_identity"] != identity["digest"] or existing["source_key"] != source_key
                        or existing["match_id"] != match_id
                        or existing["provenance"].get("requested_language") != language):
                    raise CaptionError("existing_track_identity_mismatch")
                if any(start < interval["end_seconds"] and end > interval["start_seconds"]
                       for interval in existing["coverage_intervals"]):
                    raise CaptionError("coverage_already_generated")
            reserved = sum(record["estimated_cost_usd"] for record in jobs.values())
            if len(jobs) >= 1000:
                raise CaptionError("ledger_job_limit")
            if reserved + cost > max_cost_usd + 1e-12:
                raise CaptionError("cumulative_cost_budget_exceeded")
            # Completed tracks need neither credentials nor provider access.
            # A new job still cannot reserve money or decode without a key.
            if self.transcriber is None and not os.environ.get("ELEVENLABS_API_KEY"):
                raise CaptionError("api_key_missing")
            jobs[job] = {**plan, "status": "reserved"}
            atomic_write(ledger_path, canonical_json(ledger))
            # Reserve BEFORE audio/network processing. Uncertain failures remain
            # reserved so rerunning a command cannot silently pay twice.
            try:
                wav, audio_metadata = self.decoder(source_url, start, end, etag=identity.get("etag"))
                response = (self.transcriber or ElevenLabsTranscriber()).transcribe(wav, model=model, language=language)
                track = words_to_track(response, match_id=match_id, source_key=source_key,
                                          identity=identity["digest"], start=start, end=end,
                                          language=language, audio_metadata=audio_metadata)
                if existing:
                    if existing["language"] != track["language"]:
                        raise CaptionError("provider_language_changed")
                    track["cues"] = sorted(existing["cues"] + track["cues"], key=lambda cue: cue["start_seconds"])
                    track["coverage_intervals"] = sorted(existing["coverage_intervals"] + track["coverage_intervals"],
                                                         key=lambda interval: interval["start_seconds"])
                    track["provenance"]["audio_windows"] = (existing["provenance"]["audio_windows"]
                                                               + track["provenance"]["audio_windows"])
                validate_track(track)
                atomic_write(existing_path, canonical_json(track))
                atomic_write(self.directory / f"{filename}.vtt", webvtt(track))
                jobs[job]["status"] = "complete"
                atomic_write(ledger_path, canonical_json(ledger))
                return {**plan, "status": "complete", "job_id": job, "cue_count": len(track["cues"])}
            except Exception as exc:
                jobs[job]["status"] = "failed_reserved"
                jobs[job]["error_code"] = exc.code if isinstance(exc, CaptionError) else type(exc).__name__
                atomic_write(ledger_path, canonical_json(ledger))
                if isinstance(exc, CaptionError):
                    raise
                raise CaptionError("caption_generation_failed") from None
