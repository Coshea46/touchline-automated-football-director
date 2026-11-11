"""One shared fixture catalog; replay scores are independent of current standings."""

import json
import math
import re
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

DEFAULT_BUCKET = "match-switcher-poc-videos"
METADATA_PATH = Path(__file__).resolve().parent / "data" / "matches.json"


@lru_cache(maxsize=1)
def _catalog():
    data = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("matches"), list):
        raise ValueError("Unsupported fixture metadata schema")
    records = {}
    for record in data["matches"]:
        match_id = record.get("match_id")
        if (not isinstance(match_id, str) or not re.fullmatch(r"cam-[1-7]", match_id)
                or match_id in records or record.get("source_key") != f"match-{match_id[4:]}.mp4"
                or any(not isinstance(record.get(field), str) or not 1 <= len(record[field]) <= 100
                       for field in ("home_team", "away_team", "display_name"))):
            raise ValueError("Invalid fixture identity")
        previous = -1
        for interval in record.get("replay", {}).get("score_intervals", []):
            start, end = interval.get("start_seconds"), interval.get("end_seconds")
            if (any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    for value in (start, end)) or not 0 <= start < end or start < previous
                    or any(type(interval.get(field)) is not int or not 0 <= interval[field] <= 99
                           for field in ("home", "away")) or interval.get("verified") is not True):
                raise ValueError("Invalid verified replay score interval")
            previous = end
        records[match_id] = record
    if len(records) != 7:
        raise ValueError("The demo fixture catalog requires seven distinct sources")
    return records


def metadata_for_source(match_id, source_key, bucket=DEFAULT_BUCKET, *, verified_identity=None, region="eu-west-2"):
    record = _catalog().get(match_id)
    if not record or record["source_key"] != source_key:
        return None
    if bucket == DEFAULT_BUCKET:
        if verified_identity is not None and verified_identity != record.get("source_identity"):
            return None
        return deepcopy(record)
    # A same-named cam/key is insufficient. Mirror semantics require the owner's
    # explicit copy attestation plus authenticated destination metadata. Preserve
    # the original inspected identity as provenance; never invent destination IDs.
    binding = next((item for item in _source_bindings()
                    if item.get("bucket") == bucket and item.get("region") == region
                    and item.get("verification") == "operator_confirmed_unmodified_copy"
                    and item.get("reference_bucket") == DEFAULT_BUCKET), None)
    original = record.get("source_identity", {})
    if (binding is None or not isinstance(verified_identity, dict)
            or verified_identity.get("source_key") != source_key
            or verified_identity.get("content_length") != original.get("content_length")
            or not isinstance(verified_identity.get("etag"), str)
            or not verified_identity["etag"]
            or any(not isinstance(verified_identity.get(field), str)
                   or re.fullmatch(r"[a-f0-9]{64}", verified_identity[field]) is None
                   for field in ("digest", "locator_sha256"))):
        return None
    result = deepcopy(record)
    result["origin_source_identity"] = result["source_identity"]
    result["source_identity"] = deepcopy(verified_identity)
    result["source_binding"] = {
        "bucket": bucket, "region": region,
        "verification": binding["verification"], "content_length_checked": True,
        "full_file_checksum_verified": False,
    }
    return result


def is_attested_mirror(bucket, region):
    return any(item.get("bucket") == bucket and item.get("region") == region
               and item.get("verification") == "operator_confirmed_unmodified_copy"
               and item.get("reference_bucket") == DEFAULT_BUCKET
               for item in _source_bindings())


@lru_cache(maxsize=1)
def _source_bindings():
    data = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
    bindings = data.get("source_bindings", [])
    if not isinstance(bindings, list) or any(not isinstance(item, dict) for item in bindings):
        raise ValueError("Invalid source bindings")
    if len({item.get("bucket") for item in bindings}) != len(bindings):
        raise ValueError("Duplicate source binding bucket")
    return tuple(bindings)


def replay_score_at(record, source_seconds):
    if (isinstance(source_seconds, bool) or not isinstance(source_seconds, (int, float))
            or not math.isfinite(source_seconds)):
        return None
    for interval in record.get("replay", {}).get("score_intervals", []):
        if interval.get("verified") is True and interval["start_seconds"] <= source_seconds < interval["end_seconds"]:
            return {"home": interval["home"], "away": interval["away"], "kind": "replay_metadata"}
    return None
