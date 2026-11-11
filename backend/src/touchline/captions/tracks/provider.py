"""Official SDK, lazy-loaded for explicit operator checks and offline jobs."""

import os
from contextlib import contextmanager
from io import BytesIO

from touchline.captions.errors import CaptionError

SDK_VERSION = "2.77.0"
MODEL = "scribe_v2"
PRICE_PER_HOUR_USD = .22
PRICING_CHECKED = "2026-11-08"


def estimate_cost(seconds, model=MODEL):
    if model != MODEL:
        raise CaptionError("unsupported_model")
    return seconds / 3600 * PRICE_PER_HOUR_USD


def _primitive(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    raise CaptionError("provider_response_invalid")


def _safe_error(exc):
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and 400 <= status <= 599:
        return CaptionError(f"provider_http_{status}")
    return CaptionError("provider_transport_error")


def _missing_speech_to_text_permission(exc):
    """Classify the documented API error shape without exposing its message."""
    if getattr(exc, "status_code", None) not in (401, 403):
        return False
    body = getattr(exc, "body", None)
    detail = body.get("detail") if isinstance(body, dict) else None
    return (isinstance(detail, dict) and detail.get("status") == "missing_permissions"
            and isinstance(detail.get("message"), str) and "speech_to_text" in detail["message"])


class ElevenLabsTranscriber:
    def __init__(self, api_key=None, *, client=None, timeout=45):
        self._key = api_key or os.environ.get("ELEVENLABS_API_KEY")
        self._client, self.timeout = client, timeout

    @contextmanager
    def _sdk(self):
        if not self._key:
            raise CaptionError("api_key_missing")
        if self._client is not None:
            yield self._client
            return
        try:
            import httpx
            from elevenlabs.client import ElevenLabs
        except ImportError:
            raise CaptionError("elevenlabs_sdk_unavailable") from None
        # The generated client has no public close method; own its HTTP pool.
        with httpx.Client(timeout=self.timeout, follow_redirects=False) as http:
            yield ElevenLabs(api_key=self._key, timeout=self.timeout, httpx_client=http)

    def account_status(self):
        """Read-only subscription quota, without purchases or limit mutations.

        Subscription credits do not establish the scoped API-key cap or a
        model-specific credit conversion. Operators must check those too.
        """
        try:
            with self._sdk() as client:
                value = _primitive(client.user.subscription.get(
                    request_options={"max_retries": 0, "timeout_in_seconds": 10}))
            count, limit = value.get("character_count"), value.get("character_limit")
            if (isinstance(count, bool) or not isinstance(count, int) or count < 0
                    or isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
                raise CaptionError("account_quota_unverified")
            return {"provider": "elevenlabs", "tier": value.get("tier"), "status": value.get("status"),
                    "used_credits": count, "credit_limit": limit, "remaining_credits": max(0, limit - count),
                    "max_credit_limit_extension": value.get("max_credit_limit_extension"),
                    "scoped_key_quota_verified": False}
        except CaptionError:
            raise
        except Exception as exc:  # noqa: BLE001 -- SDK exceptions can contain credentials
            raise _safe_error(exc) from None

    def realtime_access_check(self):
        """Mint/discard one ephemeral token; start no audio or transcription.

        This is an explicit operator credential check, not a quota/credit check.
        Neither the token nor any provider response/error text leaves this method.
        """
        try:
            with self._sdk() as client:
                value = _primitive(client.tokens.single_use.create(
                    "realtime_scribe", request_options={"max_retries": 0, "timeout_in_seconds": 10}))
            if not isinstance(value.get("token"), str) or not value["token"]:
                raise CaptionError("provider_token_response_invalid")
            del value
            return {"status": "available", "provider": "elevenlabs", "model": "scribe_v2_realtime",
                    "check": "ephemeral_token_minted_and_discarded", "transcription_started": False}
        except CaptionError:
            raise
        except Exception as exc:  # noqa: BLE001 -- SDK error bodies can contain credentials
            if _missing_speech_to_text_permission(exc):
                raise CaptionError("provider_permission_speech_to_text_missing") from None
            raise _safe_error(exc) from None

    def transcribe(self, wav_bytes, *, model=MODEL, language="en"):
        if model != MODEL:
            raise CaptionError("unsupported_model")
        if not isinstance(wav_bytes, bytes) or not 44 < len(wav_bytes) < 25_000_000:
            raise CaptionError("invalid_audio_upload")
        try:
            with self._sdk() as client:
                result = client.speech_to_text.convert(
                    file=BytesIO(wav_bytes), model_id=MODEL, timestamps_granularity="word",
                    language_code=None if language == "auto" else language,
                    diarize=True, tag_audio_events=True, use_multi_channel=False,
                    webhook=False, request_options={"max_retries": 0, "timeout_in_seconds": self.timeout})
            value = _primitive(result)
            if not isinstance(value.get("words"), list):
                raise CaptionError("provider_word_timestamps_missing")
            return value
        except CaptionError:
            raise
        except Exception as exc:  # noqa: BLE001 -- SDK exceptions can contain credentials
            raise _safe_error(exc) from None
