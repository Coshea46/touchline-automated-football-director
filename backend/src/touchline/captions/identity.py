"""Shared source identity and canonical JSON, independent of caption execution mode."""

import hashlib
import json
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()

def source_identity(source_key, etag, content_length, source_url):
    """ETags identify object versions; multipart ETags are not media checksums.

    Query signatures are deliberately excluded from the locator digest. A new
    signed URL for the same object must not invalidate its reusable transcript.
    """
    if not isinstance(source_key, str) or not source_key or len(source_key) > 1024:
        raise ValueError("Invalid caption source key")
    if not isinstance(etag, str) or not etag.strip('"') or len(etag) > 200:
        raise ValueError("A verified source ETag is required")
    if isinstance(content_length, bool) or not isinstance(content_length, int) or content_length <= 0:
        raise ValueError("A verified source length is required")
    parsed = urlsplit(source_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid caption source locator")
    locator = urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, "", ""))
    record = {"source_key": source_key, "etag": etag.strip('"'),
              "content_length": content_length,
              "locator_sha256": hashlib.sha256(locator.encode()).hexdigest()}
    return {**record, "digest": digest(record)}

def verify_source_identity(source_key, source_url):
    """A bounded HEAD checks object version and locator without fetching media."""
    if not source_url:
        return None
    try:
        with urlopen(Request(source_url, method="HEAD"), timeout=4) as response:
            return source_identity(source_key, response.headers.get("ETag"),
                                   int(response.headers.get("Content-Length", "0")), source_url)["digest"]
    except (HTTPError, URLError, OSError, TypeError, ValueError):
        return None
