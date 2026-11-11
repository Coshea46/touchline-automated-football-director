"""Persistent, bounded encoded-media readers for the source-timed replay grid.

RGB pixels are decoded only for displayed samples. Audio remains native-rate
PCM until a completed five-second window is cropped, then resampled: the FIR
filter never receives waveform samples beyond that observation endpoint.
"""

from collections import deque
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from fractions import Fraction
from numbers import Integral, Real
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit

import numpy as np

from touchline.ml_inference.features.audio import (
    DECODER_SHA256,
    LEGACY_AUDIO,
    SAMPLECLOCK_AUDIO,
    clock_tolerance,
    validate_aligned_pcm,
    validate_audio_contract,
)
from touchline.ml_inference.features.validation import validate_audio

FPS = 15
AUDIO_RATE = 16000
MAX_AUDIO_BYTES = 32 * 1024 * 1024
MAX_FRAME_PIXELS = 1920 * 1080
HTTP_OPTIONS = {
    'rw_timeout': '1000000', 'reconnect': '1', 'reconnect_on_network_error': '1',
    'reconnect_on_http_error': '429,5xx', 'reconnect_delay_max': '1',
    'reconnect_max_retries': '2', 'reconnect_delay_total_max': '2',
}


class MediaTimelineError(ValueError):
    """Incomplete or discontinuous source media; callers must not fill the gap."""


@dataclass
class Sample:
    sample_index: int
    rgb: np.ndarray
    source_timestamp: float


@dataclass
class _VideoPoint:
    timestamp: float
    frame: object
    duration: float
    rgb: np.ndarray | None = None


@dataclass
class _AudioChunk:
    start: int
    data: np.ndarray  # time-major native-rate float32; channels remain separate


def _number(value, name, minimum=0):
    if (isinstance(value, bool) or not isinstance(value, Real)
            or not np.isfinite(value) or value < minimum):
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return float(value)


def _source(value, allow_local):
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError("A media source is required")
    source = str(value)
    try:
        parsed = urlsplit(source)
        if parsed.scheme in ("http", "https"):
            if not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Media HTTP source is invalid")
            return source
        if allow_local and Path(source).is_file():
            return str(Path(source).resolve())
    except (ValueError, OSError):
        raise ValueError("Media source is invalid") from None
    raise ValueError("Media source must use HTTP(S); local fixtures require allow_local=True")


def _native_pcm(frame):
    channels = len(frame.layout.channels)
    if channels not in (1, 2):
        raise MediaTimelineError("Source audio must contain one or two channels")
    raw = frame.to_ndarray()
    data = raw.T if frame.format.is_planar else raw.reshape(-1, channels)
    if data.shape != (frame.samples, channels):
        raise MediaTimelineError("Decoded audio shape disagrees with its sample count")
    if np.issubdtype(data.dtype, np.signedinteger):
        data = data.astype(np.float32) / float(2 ** (np.iinfo(data.dtype).bits - 1))
    elif np.issubdtype(data.dtype, np.unsignedinteger):
        centre = float(2 ** (np.iinfo(data.dtype).bits - 1))
        data = (data.astype(np.float32) - centre) / centre
    elif np.issubdtype(data.dtype, np.floating):
        data = data.astype(np.float32, copy=False)
    else:
        raise MediaTimelineError("Decoded audio has an unsupported PCM format")
    if not np.isfinite(data).all():
        raise MediaTimelineError("Decoded audio contains nonfinite samples")
    # AAC may overshoot full scale slightly; clip only AFTER resampling, matching
    # the research decoder. Clipping native samples first changes the filter.
    return np.ascontiguousarray(data)


class MediaCursor:
    """One persistent video decoder and one persistent native-audio decoder.

    ``offset_seconds`` maps common replay time to the original source timeline.
    A seek creates a new cursor and a new logical source epoch. ``pull`` may
    decode a lookahead frame to bound a displayed interval; its pixels never
    reach feature extraction before their permitted grid target.
    """

    def __init__(self, source_url, *, offset_seconds=0, start_index=0,
                 allow_local=False, max_history_seconds=12,
                 audio_clock_tolerance_seconds=0.002, av_module=None,
                 audio_contract=LEGACY_AUDIO):
        self.audio_contract = validate_audio_contract(audio_contract)
        self.offset_seconds = _number(offset_seconds, "offset_seconds")
        if isinstance(start_index, bool) or not isinstance(start_index, Integral) or start_index < 0:
            raise ValueError("start_index must be a nonnegative integer")
        self.next_index = self._initial_index = int(start_index)
        self.max_history_seconds = _number(max_history_seconds, "max_history_seconds", 5)
        # The existing S3 AAC files retain <=1.18ms of source-clock jitter after
        # remuxing into a 1/sample_rate time base. That finer declared time base
        # does not recover the original timestamp precision. Anchor the native
        # sample clock once and accept at most2ms of bounded discrepancy; never
        # reset it, stretch PCM, insert silence or bridge a missing AAC packet.
        self.audio_clock_tolerance_seconds = _number(
            audio_clock_tolerance_seconds, "audio_clock_tolerance_seconds",
        )
        if self.audio_clock_tolerance_seconds > 0.002:
            raise ValueError("Audio clock quantization tolerance cannot exceed two milliseconds")
        source = _source(source_url, allow_local)
        if av_module is None:
            import av as av_module

        self._av = av_module
        self._lock, self._pull_lock = RLock(), RLock()
        self._video_container = self._audio_container = None
        self._closed = self._failed = self.eof = False
        self._video_previous = self._video_lookahead = None
        self._last_video_pts = None
        self._video_eof = False
        self._audio_eof = False
        self._audio_chunks = deque()
        self._audio_bytes = 0
        self._audio_rate = None
        self._audio_clock = None
        self._audio_end = None
        self._audio_evicted_before = None
        self._audio_max_clock_deviation = 0
        self._audio_clock_corrections = self._audio_real_discontinuities = 0
        self._audio_gap_history = deque(maxlen=16)
        self._last_permit = None
        self._primed = False
        self._last_av_error_type = self._last_av_error_phase = None
        open_options = {'options': dict(HTTP_OPTIONS)} if urlsplit(source).scheme in ('http', 'https') else {}
        try:
            self._video_container = self._av_call(self._av.open, source, timeout=(10, 5),
                                                   _phase='video_open', **open_options)
            if not self._video_container.streams.video:
                raise MediaTimelineError("Source contains no video stream")
            self._video_stream = self._video_container.streams.video[0]
            stream = self._video_stream
            self._video_origin = float((stream.start_time or 0) * stream.time_base)
            duration = float(stream.duration * stream.time_base) if stream.duration is not None else None
            fps = getattr(stream, "average_rate", None) or getattr(stream, "base_rate", None)
            self._fps = float(fps) if fps else None
            self._shape = (int(stream.codec_context.height), int(stream.codec_context.width))
            if min(self._shape) < 16:
                raise MediaTimelineError("Video dimensions must each be at least 16 pixels")
            if self._shape[0] * self._shape[1] > MAX_FRAME_PIXELS:
                raise MediaTimelineError("Video exceeds the 2073600-pixel frame limit")
            stream.codec_context.thread_count = 1
            self._audio_container = self._av_call(self._av.open, source, timeout=(10, 5),
                                                   _phase='audio_open', **open_options)
            if not self._audio_container.streams.audio and self.audio_contract == LEGACY_AUDIO:
                raise MediaTimelineError("Source contains no audio stream")
            self._audio_stream = (self._audio_container.streams.audio[0]
                                  if self._audio_container.streams.audio else None)
            if self._audio_stream is not None:
                self._audio_stream.codec_context.thread_count = 1
            self._channels = len(self._audio_stream.codec_context.layout.channels) if self._audio_stream else 1
            if self._channels not in (1, 2):
                raise MediaTimelineError("Source audio must contain one or two channels")
            initial_source_time = self.offset_seconds + self.next_index / FPS
            video_seek = max(0, initial_source_time - 0.25) + self._video_origin
            self._av_call(self._video_container.seek, int(video_seek / stream.time_base), stream=stream,
                          backward=True, _phase='video_seek')
            audio_seek = max(0, self._video_origin + initial_source_time - 0.25)
            if self._audio_stream is not None:
                self._av_call(self._audio_container.seek, int(audio_seek * self._av.time_base), backward=True,
                              _phase='audio_seek')
            self._video_frames = iter(self._av_call(self._video_container.decode, self._video_stream))
            self._audio_frames = (iter(self._av_call(self._audio_container.decode, self._audio_stream))
                                  if self._audio_stream else iter(()))
            self.metadata = {
                "duration_seconds": duration,
                "replay_duration_seconds": None if duration is None else max(0.0, duration - self.offset_seconds),
                "fps": self._fps, "width": self._shape[1], "height": self._shape[0],
                "max_frame_pixels": MAX_FRAME_PIXELS,
                "decoder_threads": 1, "open_timeout_seconds": 10, "read_timeout_seconds": 5,
                "has_audio": self._audio_stream is not None, "audio_channels": self._channels,
                "audio_sample_rate": (getattr(self._audio_stream.codec_context, "sample_rate", None)
                                      if self._audio_stream else None),
                "source_offset_seconds": self.offset_seconds,
                "video_origin_seconds": self._video_origin,
                "audio_clock_tolerance_seconds": self.audio_clock_tolerance_seconds,
                "audio_contract": self.audio_contract,
                "audio_decoder_sha256": DECODER_SHA256 if self.audio_contract == SAMPLECLOCK_AUDIO else None,
            }
        except Exception as exc:
            self.close()
            if isinstance(exc, MediaTimelineError):
                raise
            raise self._safe_error('open', exc) from None

    def _safe_error(self, phase, exc):
        error = RuntimeError(f"Media {phase} failed ({type(exc).__name__})")
        error.media_error_type = self._last_av_error_type or type(exc).__name__
        error.media_error_phase = self._last_av_error_phase or phase
        return error

    def _av_call(self, function, *args, _phase='avio', **kwargs):
        # FFmpeg can log an HTTP error containing a presigned URL before PyAV
        # raises it. Thread-local Capture discards those raw log records as well
        # as sanitizing the Python exception at each public boundary.
        logging = getattr(self._av, "logging", None)
        capture = getattr(logging, "Capture", None)
        try:
            with capture(local=True) if capture is not None else nullcontext():
                return function(*args, **kwargs)
        except (StopIteration, EOFError):
            raise
        except Exception as exc:
            with self._lock:
                self._last_av_error_type = type(exc).__name__
                self._last_av_error_phase = _phase
            raise

    def _read_video(self):
        try:
            frame = self._av_call(next, self._video_frames, _phase='video_decode')
        except StopIteration:
            self._video_eof = True
            return None
        if frame.pts is None:
            raise MediaTimelineError("Video frame lacks a presentation timestamp")
        timestamp = float(frame.pts * self._video_stream.time_base) - self._video_origin
        if self._last_video_pts is not None and timestamp <= self._last_video_pts:
            raise MediaTimelineError("Video presentation timestamps are not strictly increasing")
        self._last_video_pts = timestamp
        duration = float(frame.duration * self._video_stream.time_base) if frame.duration else 0.0
        return _VideoPoint(timestamp, frame, duration)

    def _sample(self, index):
        return self._displayed(self.offset_seconds + (index + 0.5) / FPS, index)

    def _displayed(self, target, index):
        while self._video_lookahead is None or self._video_lookahead.timestamp <= target + 1e-8:
            if self._video_lookahead is not None:
                point = self._video_lookahead
                if point.frame.is_corrupt or (point.frame.height, point.frame.width) != self._shape:
                    raise MediaTimelineError("Video frame is corrupt or changes dimensions")
                self._video_previous, self._video_lookahead = point, None
            if self._video_eof:
                break
            self._video_lookahead = self._read_video()
            if self._video_lookahead is None:
                break
        point = self._video_previous
        if point is None:
            raise MediaTimelineError("Video starts after the requested sampling interval")
        if target - point.timestamp > 0.21 + 1e-8:
            if self._video_eof:
                raise EOFError("Media video reached its endpoint")
            raise MediaTimelineError("Video presentation timestamp gap exceeds 210ms")
        if self._video_eof and point.timestamp + point.duration < target - 0.01:
            raise EOFError("Media video reached its endpoint")
        if point.rgb is None:
            point.rgb = np.ascontiguousarray(self._av_call(point.frame.to_ndarray, format="rgb24", _phase='video_rgb'))
            if point.rgb.dtype != np.uint8 or point.rgb.shape != (*self._shape, 3):
                raise MediaTimelineError("Decoded video is not uint8 RGB")
            point.rgb.flags.writeable = False
        return Sample(index, point.rgb, point.timestamp)

    def _advance_audio(self, permitted_source_time):
        if self._audio_stream is None:
            return
        while self._audio_end is None or self._audio_end < permitted_source_time - 1e-9:
            if self._audio_eof:
                if self.audio_contract == SAMPLECLOCK_AUDIO:
                    return
                raise EOFError("Media audio reached its endpoint")
            try:
                frame = self._av_call(next, self._audio_frames, _phase='audio_decode')
            except StopIteration:
                self._audio_eof = True
                if self.audio_contract == SAMPLECLOCK_AUDIO:
                    return
                raise EOFError("Media audio reached its endpoint") from None
            if frame.pts is None or frame.time_base is None:
                raise MediaTimelineError("Audio frame lacks a presentation timestamp")
            rate = int(frame.sample_rate)
            if rate <= 0 or (self._audio_rate is not None and self._audio_rate != rate):
                raise MediaTimelineError("Audio sample rate changes inside a source epoch")
            self._audio_rate = rate
            source_clock = round(float(frame.pts * frame.time_base) * rate)
            clock = source_clock if self._audio_clock is None else self._audio_clock
            error = source_clock - clock
            if self.audio_contract == SAMPLECLOCK_AUDIO:
                tolerance = clock_tolerance(self._audio_stream, rate, frame.time_base)
                if abs(error) > tolerance:
                    # Preserve a genuine missing/overlapping interval on the
                    # source clock. Never stretch samples or replace it by zero.
                    self._audio_real_discontinuities += 1
                    self._audio_gap_history.append({'at_seconds': source_clock / rate - self._video_origin,
                                                    'gap_seconds': error / rate})
                    clock = source_clock
                elif error:
                    self._audio_clock_corrections += 1
                    self._audio_max_clock_deviation = max(self._audio_max_clock_deviation, abs(error))
            else:
                tolerance = max(int(np.ceil(float(frame.time_base) * rate)) + 1,
                                int(np.ceil(self.audio_clock_tolerance_seconds * rate)))
                if abs(error) > tolerance:
                    raise MediaTimelineError("Audio presentation timestamp discontinuity")
                self._audio_max_clock_deviation = max(self._audio_max_clock_deviation, abs(error))
            data = self._av_call(_native_pcm, frame)
            if data.shape[1] != self._channels:
                raise MediaTimelineError("Audio channel layout changes inside a source epoch")
            next_clock = clock + len(data)
            end = next_clock / rate - self._video_origin
            with self._lock:
                self._audio_chunks.append(_AudioChunk(clock, data))
                self._audio_bytes += data.nbytes
                self._audio_clock, self._audio_end = next_clock, end
                # A packet after a real gap may be far beyond the permit. It
                # is lookahead, not permission to evict the preceding waveform.
                history_end = min(end, permitted_source_time) if self.audio_contract == SAMPLECLOCK_AUDIO else end
                cutoff = history_end - self.max_history_seconds
                while self._audio_chunks and (
                    (self._audio_chunks[0].start + len(self._audio_chunks[0].data)) / rate
                        - self._video_origin <= cutoff
                ):
                    removed = self._audio_chunks.popleft()
                    self._audio_bytes -= removed.data.nbytes
                    self._audio_evicted_before = (removed.start + len(removed.data)) / rate
                if self._audio_bytes > MAX_AUDIO_BYTES:
                    self._audio_chunks.clear()
                    self._audio_bytes = 0
                    raise MemoryError("Native audio history exceeds 32MiB")

    def prime(self, permitted_position):
        """Prepare only the frame displayed at the initial clock, without sampling.

        In particular this uses index/15, not the next interval centre. A future
        frame may be decoded as ordinary lookahead, but its pixels never enter
        RGB conversion, YOLO or observations during startup.
        """
        permit = _number(permitted_position, 'permitted_position')
        base = self._initial_index / FPS
        if permit + 1e-8 < base:
            raise ValueError('Cannot prime a future replay position')
        with self._pull_lock:
            if self._closed or self._failed or self.next_index != self._initial_index:
                raise RuntimeError('Media priming requires an unopened sampling epoch')
            try:
                self._displayed(self.offset_seconds + base, self._initial_index)
                self._advance_audio(self.offset_seconds + base)
                self._primed = True
            except EOFError:
                raise
            except Exception as exc:
                self._failed = True
                if isinstance(exc, MediaTimelineError):
                    raise
                raise self._safe_error('prime', exc) from None

    def pull(self, permitted_position, limit=16):
        """Return at most ``limit`` newly permitted 15Hz samples, in source order."""
        permit = _number(permitted_position, "permitted_position")
        if isinstance(limit, bool) or not isinstance(limit, Integral) or not 1 <= limit <= 75:
            raise ValueError("limit must be an integer between one and 75")
        with self._pull_lock:
            if self._closed or self._failed:
                raise RuntimeError("Media cursor is closed or failed; reopen for a new source epoch")
            if self.eof:
                raise EOFError("Media video reached its endpoint")
            if self._last_permit is not None and permit < self._last_permit - 1e-8:
                raise ValueError("Replay position moved backwards; reopen the media cursor")
            self._last_permit = permit
            if permit < self._initial_index / FPS:
                return []
            samples = []
            try:
                while len(samples) < limit and (self.next_index + 0.5) / FPS <= permit + 1e-8:
                    try:
                        sample = self._sample(self.next_index)
                    except EOFError:
                        self.eof = True
                        if not samples:
                            raise
                        break
                    samples.append(sample)
                    self.next_index += 1
                audio_permit = self.offset_seconds + permit
                if self.eof and self._video_previous is not None:
                    audio_permit = min(audio_permit, self._video_previous.timestamp + self._video_previous.duration)
                try:
                    self._advance_audio(audio_permit)
                except EOFError:
                    if not samples:
                        raise
                return samples
            except EOFError:
                raise
            except Exception as exc:
                self._failed = True
                if isinstance(exc, MediaTimelineError):
                    raise
                raise self._safe_error('decode', exc) from None

    def audio_window(self, start_common, end_common):
        """Crop a complete native-PCM window before resampling, preserving stereo."""
        start = _number(start_common, "start_common")
        end = _number(end_common, "end_common")
        if abs(end - start - 5) > 1e-8:
            raise ValueError("Audio inference windows must span exactly five seconds")
        with self._lock:
            if self._closed or self._failed:
                raise RuntimeError("Media cursor is closed or failed")
            if self._last_permit is None or end > self._last_permit + 1e-8:
                raise MediaTimelineError("Audio window extends beyond the permitted replay position")
            chunks, rate = list(self._audio_chunks), self._audio_rate
            evicted_before = self._audio_evicted_before
        if self._audio_stream is None and self.audio_contract == SAMPLECLOCK_AUDIO:
            return None
        if not chunks or rate is None:
            if self.audio_contract == SAMPLECLOCK_AUDIO and self._audio_eof:
                missing = np.full((80000, self._channels), np.nan, np.float32)
                return missing[:, 0] if self._channels == 1 else missing
            raise MediaTimelineError("Audio window has not been decoded")
        source_start = self._video_origin + self.offset_seconds + start
        source_end = self._video_origin + self.offset_seconds + end
        if evicted_before is not None and source_start < evicted_before - 1e-8:
            raise MediaTimelineError("Audio window history was evicted")
        waveform = np.full((80000, self._channels), np.nan, np.float32)
        try:
            resampler = self._av_call(self._av.AudioResampler,
                format="fltp", layout="mono" if self._channels == 1 else "stereo", rate=AUDIO_RATE,
            )

            def store(converted):
                if converted.pts is None or converted.time_base is None:
                    raise MediaTimelineError("Resampled audio lacks a presentation timestamp")
                offset = round((float(converted.pts * converted.time_base) - source_start) * AUDIO_RATE)
                data = np.clip(converted.to_ndarray().T, -1.0, 1.0)
                a, b = max(0, offset), min(80000, offset + len(data))
                if b > a:
                    waveform[a:b] = data[a - offset:b - offset]

            for chunk in chunks:
                timestamp = chunk.start / rate
                a = max(0, int(np.ceil((source_start - timestamp) * rate - 1e-7)))
                b = min(len(chunk.data), int(np.ceil((source_end - timestamp) * rate - 1e-7)))
                if b <= a:
                    continue
                cropped = np.ascontiguousarray(chunk.data[a:b].T)
                frame = self._av_call(self._av.AudioFrame.from_ndarray,
                    cropped, format="fltp", layout="mono" if self._channels == 1 else "stereo",
                )
                frame.sample_rate, frame.time_base, frame.pts = rate, Fraction(1, rate), chunk.start + a
                for converted in self._av_call(resampler.resample, frame):
                    store(converted)
            for converted in self._av_call(resampler.resample, None):
                store(converted)
            if self.audio_contract == LEGACY_AUDIO and not np.isfinite(waveform).all():
                raise MediaTimelineError("Audio window has incomplete coverage or was evicted")
            result = waveform[:, 0] if self._channels == 1 else waveform
            (validate_aligned_pcm if self.audio_contract == SAMPLECLOCK_AUDIO else validate_audio)(result)
            return result
        except MediaTimelineError:
            raise
        except Exception as exc:  # noqa: BLE001 -- decoder errors can contain private source URLs.
            raise RuntimeError(f"Media audio resampling failed ({type(exc).__name__})") from None

    @property
    def stats(self):
        with self._lock:
            return {
                "next_sample_index": self.next_index, "audio_history_bytes": self._audio_bytes,
                "primed": self._primed, "last_av_error_type": self._last_av_error_type,
                "last_av_error_phase": self._last_av_error_phase,
                "audio_history_chunks": len(self._audio_chunks), "audio_end_seconds": self._audio_end,
                "audio_contract": self.audio_contract,
                "audio_clock_corrections": self._audio_clock_corrections,
                "audio_real_discontinuities": self._audio_real_discontinuities,
                "audio_recent_discontinuities": list(self._audio_gap_history),
                "audio_max_clock_deviation_seconds": (
                    self._audio_max_clock_deviation / self._audio_rate if self._audio_rate else 0.0
                ),
                "rgb_history_frames": int(self._video_previous is not None and self._video_previous.rgb is not None),
                "eof": self.eof,
            }

    def close(self):
        with self._pull_lock:
            if self._closed:
                return
            self._closed = True
            for container in (self._video_container, self._audio_container):
                if container is not None:
                    # Cleanup must finish even if a decoder reports an error.
                    # Its exception may include private source credentials.
                    with suppress(Exception):
                        self._av_call(container.close)
            with self._lock:
                self._audio_chunks.clear()
                self._audio_bytes = 0
                self._video_previous = self._video_lookahead = None
