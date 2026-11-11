"""Live caption connection, retaining the bounded focused-audio contract."""

import os
from touchline.captions.live.events import compact_transcript


LIVE_MODEL = "scribe_v2_realtime"

PROVIDER_ERROR_CODES = frozenset({
    "auth_error", "quota_exceeded", "rate_limited", "unaccepted_terms", "input_error", "invalid_request",
    "transcriber_error", "commit_throttled", "queue_overflow", "resource_exhausted",
    "session_time_limit_exceeded", "chunk_size_exceeded", "insufficient_audio_activity",
})

class BufferedRealtimeConnection:
    """Attach SDK listeners before its newly scheduled receiver can run.

    The SDK schedules its receiver immediately before returning from connect.
    This adapter is installed in that same coroutine stack, with no intervening
    await/task wrapper. Later Engine registration replays at most one latest
    event per supported kind. Error text, URLs and close reasons are discarded.
    """

    requires_session_started = True

    def __init__(self, connection):
        self._connection = connection
        self._handlers, self._pending = {}, {}
        self.websocket = connection.websocket
        for name in ("session_started", "partial_transcript", "committed_transcript",
                     "committed_transcript_with_timestamps", "error", "close"):
            connection.on(name, lambda value=None, event=name: self._capture(event, value))
        for name in PROVIDER_ERROR_CODES:
            connection.on(name, lambda value=None, event=name: self._capture("error", {"message_type": event}))

    def _capture(self, name, value):
        if name == "error":
            kind = value.get("message_type") if isinstance(value, dict) else None
            value = {"message_type": kind if isinstance(kind, str) and kind in PROVIDER_ERROR_CODES else "provider_error"}
        elif name == "close":
            code = value.get("code") if isinstance(value, dict) else None
            value = {"code": code if type(code) is int and 1000 <= code <= 4999 else None}
        elif name == "session_started":
            value = {"message_type": "session_started"}
        else:
            if not isinstance(value, dict) or not isinstance(value.get("text", ""), str):
                return
            value = compact_transcript(value)
        handlers = self._handlers.get(name, ())
        if handlers:
            for handler in tuple(handlers):
                handler(value)
        else:
            self._pending[name] = value

    def on(self, name, callback):
        self._handlers.setdefault(name, []).append(callback)
        if name in self._pending:
            callback(self._pending.pop(name))

    async def send(self, value):
        await self._connection.send(value)

    async def close(self):
        await self._connection.close()

async def official_connect(control):
    import httpx
    from elevenlabs import AudioFormat, CommitStrategy
    from elevenlabs.client import ElevenLabs

    http = httpx.Client(timeout=10, follow_redirects=False)
    client = ElevenLabs(api_key=os.environ.get("ELEVENLABS_API_KEY"), httpx_client=http)
    try:
        # The Engine's outer deadline already bounds this await. An inner
        # wait_for creates a task boundary where SDK errors can arrive before
        # listeners are installed. Direct await resumes here synchronously.
        raw = await client.speech_to_text.realtime.connect({
            "model_id": LIVE_MODEL, "audio_format": AudioFormat.PCM_16000, "sample_rate": 16000,
            "commit_strategy": CommitStrategy.VAD, "vad_silence_threshold_secs": .7,
            "include_timestamps": True, "language_code": "en"})
        connection = BufferedRealtimeConnection(raw)
    except BaseException:
        http.close()
        raise
    connection._caption_http_pool = http
    return connection
