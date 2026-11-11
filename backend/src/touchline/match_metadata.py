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


def metadata_for_source(match_id, source_key, bucket=DEFAULT_BUCKET):
    if bucket != DEFAULT_BUCKET:
        return None
    record = _catalog().get(match_id)
    return deepcopy(record) if record and record["source_key"] == source_key else None


def replay_score_at(record, source_seconds):
    if (isinstance(source_seconds, bool) or not isinstance(source_seconds, (int, float))
            or not math.isfinite(source_seconds)):
        return None
    for interval in record.get("replay", {}).get("score_intervals", []):
        if interval.get("verified") is True and interval["start_seconds"] <= source_seconds < interval["end_seconds"]:
            return {"home": interval["home"], "away": interval["away"], "kind": "replay_metadata"}
    return None
