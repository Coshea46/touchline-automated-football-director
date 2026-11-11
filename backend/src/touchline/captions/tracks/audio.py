"""Finite source-audio extraction; no video decoding or future caption timing."""

import wave
from contextlib import nullcontext
from fractions import Fraction
from hashlib import sha256
from io import BytesIO

from touchline.captions.errors import CaptionError


def decode_audio(source_url, start_seconds, end_seconds, *, etag=None):
    """Crop native mono/stereo audio before 16kHz conversion, without gap fill.

    Existing authorized remuxed AAC sources retain up to 2ms of packet-clock
    quantization. The anchored sample clock never resets to hide a real gap.
    """
    try:
        import av
        import numpy as np
    except ImportError:
        raise CaptionError("audio_decoder_unavailable") from None
    count = round((end_seconds - start_seconds) * 16000)
    if not 0 < count <= 30 * 16000:
        raise CaptionError("audio_duration_limit")
    options = {"rw_timeout": "5000000"}
    if etag:
        options["headers"] = f'If-Match: "{etag.strip(chr(34))}"\r\n'
    capture = getattr(getattr(av, "logging", None), "Capture", None)
    context = capture(local=True) if capture else nullcontext()
    try:
        with context, av.open(source_url, timeout=(10, 5), options=options) as container:
            if not container.streams.audio or not container.streams.video:
                raise CaptionError("source_audio_or_clock_missing")
            video = container.streams.video[0]
            origin = float(video.start_time * video.time_base) if video.start_time is not None else 0.0
            stream = container.streams.audio[0]
            stream.codec_context.thread_count = 1
            channels = len(stream.codec_context.layout.channels)
            if channels not in (1, 2):
                raise CaptionError("unsupported_audio_channels")
            container.seek(max(0, int((origin + start_seconds - .25) * av.time_base)), backward=True)
            resampler = av.AudioResampler(format="fltp", layout="mono" if channels == 1 else "stereo", rate=16000)
            waveform = np.full((count, channels), np.nan, np.float32)
            clock, input_rate, max_jitter = None, None, 0.0

            def store(converted):
                if converted.pts is None:
                    raise CaptionError("audio_timestamp_missing")
                offset = round((float(converted.pts * converted.time_base) - origin - start_seconds) * 16000)
                data = np.clip(converted.to_ndarray().T, -1, 1)
                a, b = max(0, offset), min(count, offset + len(data))
                if b > a:
                    waveform[a:b] = data[a - offset:b - offset]

            for frame in container.decode(stream):
                if frame.pts is None or not frame.sample_rate:
                    raise CaptionError("audio_timestamp_missing")
                if input_rate is not None and input_rate != frame.sample_rate:
                    raise CaptionError("audio_rate_changed")
                input_rate = frame.sample_rate
                source_time = float(frame.pts * frame.time_base)
                if clock is None:
                    clock = round(source_time * input_rate)
                jitter = abs(round(source_time * input_rate) - clock) / input_rate
                max_jitter = max(max_jitter, jitter)
                tolerance = max(.002, float(frame.time_base)) + 1 / input_rate
                if jitter > tolerance:
                    raise CaptionError("audio_discontinuity")
                frame_start, clock = clock, clock + frame.samples
                t = frame_start / input_rate
                if t - origin >= end_seconds:
                    break
                a = max(0, int(np.ceil((origin + start_seconds - t) * input_rate - 1e-7)))
                b = min(frame.samples, int(np.ceil((origin + end_seconds - t) * input_rate - 1e-7)))
                if b <= a:
                    continue
                raw = frame.to_ndarray()
                cropped = raw[:, a:b] if frame.format.is_planar else raw[:, a * channels:b * channels]
                normalized = av.AudioFrame.from_ndarray(np.ascontiguousarray(cropped),
                                                         format=frame.format.name, layout=frame.layout.name)
                normalized.sample_rate, normalized.time_base = input_rate, Fraction(1, input_rate)
                normalized.pts = frame_start + a
                for converted in resampler.resample(normalized):
                    store(converted)
            for converted in resampler.resample(None):
                store(converted)
        if not np.isfinite(waveform).all():
            raise CaptionError("audio_coverage_missing")
        buffer = BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(channels)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(np.rint(waveform * 32767).astype("<i2").tobytes())
        data = buffer.getvalue()
        return data, {"sample_rate": 16000, "channels": channels, "sample_count": count,
                      "channel_policy": "preserved", "audio_sha256": sha256(data).hexdigest(),
                      "max_source_clock_jitter_seconds": max_jitter,
                      "source_clock_quantization_tolerance_seconds": .002}
    except CaptionError:
        raise
    except Exception as exc:  # noqa: BLE001 -- external decoder exceptions must never expose signed URLs
        raise CaptionError(f"audio_decode_{type(exc).__name__}") from None
