"""Run with ``python -m touchline.captions --match-id cam-1 --start 120``.

Default is an estimate. --execute is a paid action, hard capped at 30 seconds.
The ledger's total reserved estimate must fit --max-cost-usd, including failures.
"""

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from touchline.config.local import load_local_environment
from touchline.match_metadata import METADATA_PATH
from touchline.replay.sources import SourceRegistry

from touchline.captions.tracks.pipeline import CaptionPipeline
from touchline.captions.tracks.provider import MODEL, ElevenLabsTranscriber
from touchline.captions.errors import CaptionError
from touchline.captions.identity import source_identity


def head_identity(source, *, opener=urlopen):
    try:
        request = Request(source.source_url, method="HEAD")
        with opener(request, timeout=10) as response:
            return source_identity(source.key, response.headers.get("ETag"),
                                   int(response.headers.get("Content-Length", "0")), source.source_url)
    except (HTTPError, URLError, OSError, ValueError, TypeError):
        raise CaptionError("source_identity_verification_failed") from None


def main(argv=None):
    load_local_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match-id")
    parser.add_argument("--start", type=float, default=120)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--model", choices=(MODEL,), default=MODEL)
    parser.add_argument("--language", default="en")
    parser.add_argument("--max-seconds", type=float, default=30)
    parser.add_argument("--max-cost-usd", type=float, default=.003)
    parser.add_argument("--track-dir")
    parser.add_argument("--execute", action="store_true")
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--account-check", action="store_true", help="read-only ElevenLabs subscription quota check")
    checks.add_argument("--realtime-access-check", action="store_true",
                        help="mint/discard one ephemeral realtime token to check permission; no transcription")
    args = parser.parse_args(argv)
    try:
        if args.account_check:
            print(json.dumps(ElevenLabsTranscriber().account_status(), allow_nan=False))
            return 0
        if args.realtime_access_check:
            print(json.dumps(ElevenLabsTranscriber().realtime_access_check(), allow_nan=False))
            return 0
        if not args.match_id:
            raise CaptionError("match_id_required")
        sources = SourceRegistry.from_env().resolve()
        source = next((item for item in sources if item.match_id == args.match_id), None)
        if source is None or not source.key:
            raise CaptionError("unknown_source")
        # Estimates can use previously verified metadata and do no network I/O.
        identity = None
        if METADATA_PATH.is_file():
            metadata = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
            identity = next((item.get("source_identity") for item in metadata["matches"]
                             if item["match_id"] == source.match_id and item["source_key"] == source.key), None)
        if args.execute:
            # Object-version verification needs no transcription key. The
            # pipeline may reuse a completed track without provider access.
            identity = head_identity(source)
        if not isinstance(identity, dict) or not identity.get("digest"):
            raise CaptionError("source_identity_unverified")
        result = CaptionPipeline(args.track_dir).generate(
            match_id=source.match_id, source_key=source.key, identity=identity,
            source_url=source.source_url, start_seconds=args.start,
            end_seconds=args.start + args.seconds, model=args.model, language=args.language,
            max_seconds=args.max_seconds, max_cost_usd=args.max_cost_usd, execute=args.execute)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except CaptionError as exc:
        print(json.dumps({"status": "unavailable", "reason": exc.code}))
        return 2
    except (ValueError, TypeError, KeyError, OSError):
        print(json.dumps({"status": "unavailable", "reason": "caption_configuration_invalid"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
