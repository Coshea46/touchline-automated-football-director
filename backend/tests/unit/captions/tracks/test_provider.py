"""Offline tests for unit captions tracks test_provider."""


import json
from types import SimpleNamespace
from urllib.error import URLError
import pytest
from touchline.captions.tracks.pipeline import words_to_track
from touchline.captions.errors import CaptionError
from touchline.captions.tracks.provider import ElevenLabsTranscriber


@pytest.mark.parametrize("response", [
    {"text": "untimed text"}, {"text": "untimed text", "words": []},
    {"words": [{"start": -1, "end": 2, "text": "bad", "type": "word"}]},
    {"words": [{"start": 0, "end": 31, "text": "bad", "type": "word"}]},
    {"words": [{"start": 2, "end": 3, "text": "a", "type": "word"},
               {"start": 1, "end": 4, "text": "b", "type": "word"}]},
])
def test_provider_timestamps_never_guessed_or_repaired(response, identity):
    with pytest.raises(CaptionError):
        words_to_track(response, match_id="cam-1", source_key="match-1.mp4", identity=identity["digest"],
                          start=120, end=150, language="en", audio_metadata={})


def test_actual_provider_silence_has_coverage_and_no_invented_cues(identity):
    track = words_to_track({"text": "", "words": []}, match_id="cam-1", source_key="match-1.mp4",
                              identity=identity["digest"], start=120, end=150, language="en", audio_metadata={})
    assert not track["cues"] and track["coverage_intervals"]


def test_provider_native_timestamp_request_and_sanitized_single_attempt():
    requests = []
    def convert(**kwargs):
        requests.append(kwargs)
        assert kwargs["request_options"] == {"max_retries": 0, "timeout_in_seconds": 45}
        assert kwargs["model_id"] == "scribe_v2" and kwargs["timestamps_granularity"] == "word"
        assert kwargs["diarize"] and kwargs["tag_audio_events"] and not kwargs["use_multi_channel"]
        return {"words": [], "text": ""}
    client = SimpleNamespace(speech_to_text=SimpleNamespace(convert=convert))
    provider = ElevenLabsTranscriber("private-key", client=client)
    assert provider.transcribe(b"x" * 100)["words"] == []
    assert len(requests) == 1
    def failure(*a, **k):
        raise URLError("PRIVATE_URL_AND_KEY")
    with pytest.raises(CaptionError, match="^provider_transport_error$"):
        ElevenLabsTranscriber("private-key", client=SimpleNamespace(speech_to_text=SimpleNamespace(convert=failure))).transcribe(b"x" * 100)
    class SDKError(Exception):
        status_code = 429
    def http_failure(*a, **k):
        raise SDKError("PRIVATE_BODY")
    with pytest.raises(CaptionError, match="^provider_http_429$"):
        ElevenLabsTranscriber("private-key", client=SimpleNamespace(speech_to_text=SimpleNamespace(convert=http_failure))).transcribe(b"x" * 100)


def test_account_check_uses_read_only_sdk_and_returns_no_private_account_fields():
    requests = []
    def get(**kwargs):
        requests.append(kwargs)
        return {"tier": "free", "status": "active", "character_count": 100, "character_limit": 10000,
                "max_credit_limit_extension": 0, "email": "PRIVATE_ACCOUNT"}
    client = SimpleNamespace(user=SimpleNamespace(subscription=SimpleNamespace(get=get)))
    status = ElevenLabsTranscriber("PRIVATE_KEY", client=client).account_status()
    assert status["remaining_credits"] == 9900 and status["scoped_key_quota_verified"] is False
    assert "PRIVATE" not in json.dumps(status)
    assert requests == [{"request_options": {"max_retries": 0, "timeout_in_seconds": 10}}]


def test_realtime_access_check_discards_token_without_transcription():
    requests = []
    def create(token_type, **kwargs):
        requests.append((token_type, kwargs))
        return {"token": "PRIVATE_EPHEMERAL_TOKEN"}
    client = SimpleNamespace(tokens=SimpleNamespace(single_use=SimpleNamespace(create=create)))
    result = ElevenLabsTranscriber("PRIVATE_KEY", client=client).realtime_access_check()
    assert result["status"] == "available" and result["transcription_started"] is False
    assert result["check"] == "ephemeral_token_minted_and_discarded"
    assert "PRIVATE" not in json.dumps(result) and "token" not in result
    assert requests == [("realtime_scribe", {"request_options": {"max_retries": 0, "timeout_in_seconds": 10}})]


@pytest.mark.parametrize("status,detail,code", [
    (401, {"status": "missing_permissions", "message": "PRIVATE_KEY missing speech_to_text"},
     "provider_permission_speech_to_text_missing"),
    (403, {"status": "missing_permissions", "message": "PRIVATE_KEY missing speech_to_text"},
     "provider_permission_speech_to_text_missing"),
    (401, {"status": "missing_permissions", "message": "PRIVATE_KEY missing voices_read"}, "provider_http_401"),
    (401, {"status": "invalid_api_key", "message": "PRIVATE_KEY speech_to_text"}, "provider_http_401"),
    (429, {"status": "missing_permissions", "message": "PRIVATE_KEY speech_to_text"}, "provider_http_429"),
])
def test_realtime_access_check_classifies_only_known_permission_errors(status, detail, code):
    class SDKError(Exception):
        def __init__(self):
            self.status_code, self.body = status, {"detail": detail}
            super().__init__("PRIVATE_ERROR_TEXT")
    def create(*args, **kwargs):
        raise SDKError()
    client = SimpleNamespace(tokens=SimpleNamespace(single_use=SimpleNamespace(create=create)))
    with pytest.raises(CaptionError, match=f"^{code}$"):
        ElevenLabsTranscriber("PRIVATE_KEY", client=client).realtime_access_check()


def test_realtime_access_check_rejects_empty_token_response():
    client = SimpleNamespace(tokens=SimpleNamespace(single_use=SimpleNamespace(create=lambda *a, **k: {"token": ""})))
    with pytest.raises(CaptionError, match="^provider_token_response_invalid$"):
        ElevenLabsTranscriber("PRIVATE_KEY", client=client).realtime_access_check()
