"""Check object metadata and one-byte range reads; never process match footage."""

import argparse
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from touchline.replay.sources import SourceRegistry
from touchline.config.local import load_local_environment
from touchline.storage import StorageAuthorizationError


def verify_sources(registry=None, *, opener=urlopen, now=time.time, origin="http://localhost:3030"):
    registry = registry or SourceRegistry.from_env()
    objects = []
    try:
        sources = registry.resolve()
        for source in sources:
            item = {"match_id": source.match_id, "source_key": source.key,
                    "fixture_verified": source.metadata is not None,
                    "source_identity": (source.identity or {}).get("digest"),
                    "content_length": (source.identity or {}).get("content_length"),
                    "head_ok": False, "range_ok": False, "cors_ok": False}
            try:
                with opener(Request(source.head_url or source.source_url, method="HEAD",
                                    headers={"Origin": origin}), timeout=5) as response:
                    identity = source.identity or (source.metadata or {}).get("source_identity", {})
                    item["head_ok"] = (response.headers.get("ETag", "").strip('"') == identity.get("etag")
                        and int(response.headers.get("Content-Length", "0")) == identity.get("content_length"))
                    item["content_type"] = response.headers.get("Content-Type", "")
                    head_cors = response.headers.get("Access-Control-Allow-Origin") in (origin, "*")
                with opener(Request(source.source_url, headers={"Range": "bytes=0-0", "Origin": origin}), timeout=5) as response:
                    expected = f'bytes 0-0/{item["content_length"]}'
                    item["range_ok"] = (response.getcode() == 206
                        and response.headers.get("Content-Range") == expected
                        and len(response.read(2)) == 1)
                    item["cors_ok"] = head_cors and response.headers.get("Access-Control-Allow-Origin") in (origin, "*")
            except HTTPError as exc:
                item["error"] = "source_http_error"
                item["http_status"] = exc.code
            except Exception:
                item["error"] = "source_read_check_failed"
            if source.expires_at is not None:
                item["url_valid_for_seconds"] = max(0, int(source.expires_at - now()))
            objects.append(item)
        return {"bucket": registry.bucket, "region": registry.region, "access": registry.access,
                "passed": len(objects) == 7 and all(item["head_ok"] and item["range_ok"] and item["cors_ok"]
                    and item["fixture_verified"] for item in objects),
                "browser_origin": origin,
                "verification": "authenticated_headers_and_one_byte_range",
                "full_file_checksum_verified": False, "objects": objects}
    except StorageAuthorizationError as exc:
        return {"bucket": registry.bucket, "region": registry.region, "passed": False, "error": exc.code}
    finally:
        registry.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional local JSON verification receipt")
    parser.add_argument("--origin", default="http://localhost:3030", help="Frontend origin whose S3 CORS response is checked")
    args = parser.parse_args(argv)
    try:
        load_local_environment()
        report = verify_sources(origin=args.origin)
    except Exception:
        report = {"passed": False, "error": "source_verification_unavailable"}
    encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
