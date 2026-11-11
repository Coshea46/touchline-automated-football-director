"""One progressive native decoder; only permitted PCM reaches the provider."""

import math
from contextlib import nullcontext
from dataclasses import dataclass
from fractions import Fraction

from touchline.captions.errors import CaptionError


@dataclass
class AudioChunk:
    pcm: bytes
    start_seconds: float
    end_seconds: float


class ProgressiveAudioReader:
    def __init__(self, source_url, source_start_seconds):
        import av
        import numpy as np

        self._av, self._np = av, np
        self.anchor = float(source_start_seconds)
        self._source_start = self.anchor
        self._container, self._frame, self._used = None, None, 0
        self._clock, self._input_rate = None, None
        self._pending = []
        self._next_output = None
        self._next_input_sample = None
        self.closed = self.eof = False
        self.stats = {"native_frames": 0, "pcm_seconds": 0.0, "max_cached_native_frames": 1,
                      "channel_policy": "arithmetic_mean_to_mono", "max_source_clock_jitter_seconds": 0.0}
        capture = getattr(getattr(av, "logging", None), "Capture", None)
        self._capture = (lambda: capture(local=True)) if capture else nullcontext
        try:
            with self._capture():
                self._container = av.open(source_url, timeout=(10, 5), options={"rw_timeout": "5000000"})
                if not self._container.streams.audio or not self._container.streams.video:
                    raise CaptionError("source_audio_or_clock_missing")
                video = self._container.streams.video[0]
                self._origin = float(video.start_time * video.time_base) if video.start_time is not None else 0.0
                stream = self._container.streams.audio[0]
                stream.codec_context.thread_count = 1
                self._channels = len(stream.codec_context.layout.channels)
                if self._channels not in (1, 2):
                    raise CaptionError("unsupported_audio_channels")
                self._container.seek(max(0, int((self._origin + self.anchor - .25) * av.time_base)), backward=True)
                self._iterator = iter(self._container.decode(stream))
                self._resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
        except Exception as exc:
            self.close()
            if isinstance(exc, CaptionError):
                raise
            raise CaptionError(f"audio_open_{type(exc).__name__}") from None

    def _read_frame(self):
        with self._capture():
            frame = next(self._iterator)
        if frame.pts is None or not frame.sample_rate:
            raise CaptionError("audio_timestamp_missing")
        if self._input_rate is not None and self._input_rate != frame.sample_rate:
            raise CaptionError("audio_rate_changed")
        self._input_rate = frame.sample_rate
        source_t = float(frame.pts * frame.time_base)
        if self._clock is None:
            self._clock = round(source_t * self._input_rate)
        jitter = abs(round(source_t * self._input_rate) - self._clock) / self._input_rate
        self.stats["max_source_clock_jitter_seconds"] = max(self.stats["max_source_clock_jitter_seconds"], jitter)
        if jitter > max(.002, float(frame.time_base)) + 1 / self._input_rate:
            raise CaptionError("audio_discontinuity")
        self._frame_start, self._clock = self._clock, self._clock + frame.samples
        self._frame, self._used = frame, 0
        self.stats["native_frames"] += 1

    def _convert(self, a, b):
        np, frame = self._np, self._frame
        raw = frame.to_ndarray()
        values = raw.T if frame.format.is_planar else raw.reshape(-1, self._channels)
        values = values[a:b]
        if np.issubdtype(values.dtype, np.signedinteger):
            values = values.astype(np.float32) / float(2 ** (np.iinfo(values.dtype).bits - 1))
        elif np.issubdtype(values.dtype, np.unsignedinteger):
            centre = float(2 ** (np.iinfo(values.dtype).bits - 1))
            values = (values.astype(np.float32) - centre) / centre
        else:
            values = values.astype(np.float32)
        if not np.isfinite(values).all():
            raise CaptionError("audio_nonfinite")
        mono = np.ascontiguousarray(values.mean(axis=1).reshape(1, -1))
        normalized = self._av.AudioFrame.from_ndarray(mono, format="fltp", layout="mono")
        normalized.sample_rate, normalized.time_base = self._input_rate, Fraction(1, self._input_rate)
        normalized.pts = self._frame_start + a
        if self._next_input_sample is not None and normalized.pts != self._next_input_sample:
            raise CaptionError("audio_input_discontinuity")
        for converted in self._resampler.resample(normalized):
            if converted.pts is None:
                raise CaptionError("audio_timestamp_missing")
            t = float(converted.pts * converted.time_base) - self._origin
            data = converted.to_ndarray().reshape(-1)
            if self._next_output is not None and abs(t - self._next_output) > 1.01 / 16000:
                raise CaptionError("audio_output_discontinuity")
            self._next_output = t + len(data) / 16000
            self._pending.append((t, self._np.rint(np.clip(data, -1, 1) * 32767).astype("<i2").tobytes()))
        self._next_input_sample = self._frame_start + b

    def pull(self, permitted_source_seconds, max_seconds=.25):
        if self.closed:
            raise CaptionError("audio_reader_closed")
        if not math.isfinite(permitted_source_seconds) or not 0 < max_seconds <= .5:
            raise CaptionError("audio_permit_invalid")
        if permitted_source_seconds <= self.anchor:
            return None
        target = min(permitted_source_seconds, self.anchor + max_seconds)
        try:
            while True:
                if self._frame is None or self._used >= self._frame.samples:
                    self._read_frame()
                frame_t = self._frame_start / self._input_rate - self._origin
                # A fractional viewer watermark is a permission boundary, not
                # a new audio start. Re-ceiling it on every pull skips the one
                # native sample left by the preceding floor and breaks the
                # resampler's continuous PTS. Resume at the exact input index.
                first_sample = (math.ceil((self._source_start - frame_t) * self._input_rate - 1e-7)
                                if self._next_input_sample is None else self._next_input_sample - self._frame_start)
                a = max(self._used, 0, first_sample)
                # Native samples end at or BEFORE the viewer watermark. Never
                # feed future samples into even the resampler's internal FIR.
                b = min(self._frame.samples, math.floor((target - frame_t) * self._input_rate + 1e-7))
                if b > a:
                    self._convert(a, b)
                    self._used = b
                elif frame_t + self._frame.samples / self._input_rate <= self.anchor:
                    self._used = self._frame.samples
                if self._used < self._frame.samples:
                    break
                if frame_t + self._frame.samples / self._input_rate >= target - 1e-8:
                    break
            self.anchor = target
        except StopIteration:
            self.eof = True
        except CaptionError:
            raise
        except Exception as exc:  # noqa: BLE001 -- sanitize external AV errors without exposing signed URLs
            raise CaptionError(f"audio_read_{type(exc).__name__}") from None
        if not self._pending:
            if self.eof:
                raise EOFError
            return None
        first = self._pending[0][0]
        pcm = b"".join(data for _, data in self._pending)
        self._pending.clear()
        end = first + len(pcm) / 32000
        if end > permitted_source_seconds + 1e-7:
            raise CaptionError("audio_future_samples_rejected")
        self.stats["pcm_seconds"] += len(pcm) / 32000
        return AudioChunk(pcm, first, end)

    def close(self):
        if not self.closed:
            self.closed = True
            if self._container is not None:
                with self._capture():
                    self._container.close()
