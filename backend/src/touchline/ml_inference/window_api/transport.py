"""Lossless per-frame zlib transport; no files, image codecs, resizing or pickle input."""

import zlib
from time import perf_counter

import numpy as np

from touchline.ml_inference.features.validation import validate_audio, validate_match_id, validate_window

TRANSPORT_VERSION = "matchpulse-rgb-zlib-v1"
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
AV_TRANSPORT_VERSION = "matchpulse-av-zlib-v2"


def encode_window(frames, *, max_bytes=DEFAULT_MAX_BYTES):
    validate_window(frames, max_bytes=max_bytes)
    # Compress one frame at a time to avoid an extra full-window bytes copy.
    blocks = [zlib.compress(memoryview(np.ascontiguousarray(frame)).cast("B"), level=1)
              for frame in frames]
    return {"version": TRANSPORT_VERSION, "shape": frames.shape, "blocks": blocks}


def decode_window(payload, *, max_bytes=DEFAULT_MAX_BYTES):
    if not isinstance(payload, dict) or payload.get("version") != TRANSPORT_VERSION:
        raise ValueError("Unsupported lossless transport version")
    shape = payload.get("shape")
    if (not isinstance(shape, (tuple, list)) or len(shape) != 4
            or any(isinstance(x, bool) or not isinstance(x, int) for x in shape)
            or shape[0] != 75 or shape[-1] != 3 or min(shape[1:3]) < 16):
        raise ValueError("Invalid transported video shape")
    frame_bytes = shape[1] * shape[2] * 3
    if frame_bytes * 75 > max_bytes:
        raise ValueError("Transport exceeds configured max_window_bytes")
    blocks = payload.get("blocks")
    if not isinstance(blocks, (list, tuple)) or len(blocks) != 75:
        raise ValueError("Transport requires exactly 75 compressed frames")
    output = np.empty(shape, dtype=np.uint8)
    for i, block in enumerate(blocks):
        if not isinstance(block, bytes):
            raise TypeError("Compressed frames must be bytes")
        decompressor = zlib.decompressobj()
        try:
            raw = decompressor.decompress(block, frame_bytes + 1)
        except zlib.error as exc:
            raise ValueError("Corrupt compressed video frame") from exc
        if (len(raw) != frame_bytes or not decompressor.eof
                or decompressor.unused_data or decompressor.unconsumed_tail):
            raise ValueError("Compressed video frame has invalid length or trailing data")
        output[i] = np.frombuffer(raw, dtype=np.uint8).reshape(shape[1:])
    return output


def payload_bytes(payload):
    """Compressed bytes only; excludes small metadata and Modal framing."""
    if payload.get("version") == AV_TRANSPORT_VERSION:
        return payload_bytes(payload["video"]) + len(payload["audio"]["block"])
    return sum(map(len, payload["blocks"]))


def encode_request(match_id, frames, audio, *, max_bytes=DEFAULT_MAX_BYTES):
    validate_match_id(match_id)
    validate_audio(audio)
    started = perf_counter()
    video = encode_window(frames, max_bytes=max_bytes)
    video["match_id"] = match_id
    video_seconds = perf_counter() - started
    started = perf_counter()
    block = zlib.compress(memoryview(np.ascontiguousarray(audio)).cast("B"), level=1)
    return {"version": AV_TRANSPORT_VERSION, "match_id": match_id, "video": video,
            "audio": {"match_id": match_id, "sample_rate": 16000, "dtype": np.dtype(np.float32).str,
                      "shape": audio.shape, "block": block},
            "encoding_seconds": {"video": video_seconds, "audio": perf_counter() - started}}


def decode_request(payload, *, expected_id=None, max_bytes=DEFAULT_MAX_BYTES):
    if not isinstance(payload, dict) or payload.get("version") != AV_TRANSPORT_VERSION:
        raise ValueError("Unsupported audio/video transport version")
    match_id = payload.get("match_id")
    validate_match_id(match_id)
    if expected_id is not None and match_id != expected_id:
        raise ValueError("Audio/video request match ID mismatch")
    video, audio = payload.get("video"), payload.get("audio")
    if (not isinstance(video, dict) or not isinstance(audio, dict)
            or video.get("match_id") != match_id or audio.get("match_id") != match_id):
        raise ValueError("Video and audio match IDs must match the request")
    shape = audio.get("shape")
    if (not isinstance(shape, (list, tuple)) or tuple(shape) not in ((80000,), (80000, 2))
            or any(type(x) is not int for x in shape)
            or type(audio.get("sample_rate")) is not int or audio["sample_rate"] != 16000
            or audio.get("dtype") != np.dtype(np.float32).str):
        raise ValueError("Invalid transported audio shape, dtype or sample rate")
    size = 80000 * (2 if len(shape) == 2 else 1) * 4
    block = audio.get("block")
    if not isinstance(block, bytes):
        raise TypeError("Compressed audio must be bytes")
    if len(block) > size + 65536:
        raise ValueError("Compressed audio exceeds bounded payload size")
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(block, size + 1)
    except zlib.error as exc:
        raise ValueError("Corrupt compressed audio") from exc
    if len(raw) != size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("Compressed audio has invalid length or trailing data")
    waveform = np.frombuffer(raw, dtype=np.float32).reshape(shape)
    validate_audio(waveform)
    frames = decode_window(video, max_bytes=max_bytes)
    return match_id, frames, waveform
