"""Local test-input decoding only; audio feature math stays in the research wheel."""

from fractions import Fraction


def decode_broadcast_audio(path, start, video_origin=0.0):
    """Crop to five seconds before resampling, preserving mono/stereo power.

    Normalize EVERY input frame's PTS timebase before PyAV's resampler. Mixing
    recreated endpoint frames (1/sample_rate) with MKV frames (1/1000) causes
    invalid resampler timestamps. Missing coverage remains NaN and is rejected;
    no padding, channel downmix, or gap filling is performed. MKV millisecond
    timestamps quantize packet PTS: use the continuous decoded sample clock only
    while it agrees with source PTS within that timestamp quantum. Reject actual
    discontinuities instead of filling them.
    """
    import av
    import numpy as np

    from touchline.ml_inference.features.validation import validate_audio

    end, rate = start + 5, 16000
    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise ValueError('Broadcast source has no audio')
        stream = container.streams.audio[0]
        channels = len(stream.codec_context.layout.channels)
        if channels not in (1, 2):
            raise ValueError('Test source must have mono or stereo audio')
        layout = 'mono' if channels == 1 else 'stereo'
        container.seek(max(0, int((video_origin + start - .25) * av.time_base)), backward=True)
        resampler = av.AudioResampler(format='fltp', layout=layout, rate=rate)
        waveform = np.full((80000, channels), np.nan, np.float32)
        clock_position, input_rate = None, None

        def store(frame):
            if frame.pts is None:
                raise ValueError('Resampled audio lacks timestamps')
            offset = round((float(frame.pts * frame.time_base) - video_origin - start) * rate)
            data = np.clip(frame.to_ndarray().T, -1.0, 1.0)
            a, b = max(0, offset), min(80000, offset + len(data))
            if b > a:
                waveform[a:b] = data[a - offset:b - offset]

        for frame in container.decode(stream):
            if frame.pts is None:
                raise ValueError('Source audio lacks timestamps')
            source_t = float(frame.pts * frame.time_base)
            if input_rate is not None and input_rate != frame.sample_rate:
                raise ValueError('Changing source audio sample rate is unsupported')
            input_rate = frame.sample_rate
            if clock_position is None:
                clock_position = round(source_t * input_rate)
            tolerance = int(np.ceil(float(frame.time_base) * input_rate)) + 1
            if abs(round(source_t * input_rate) - clock_position) > tolerance:
                raise ValueError(f'Audio PTS discontinuity: source={source_t:.6f} '
                                 f'expected={clock_position / input_rate:.6f} '
                                 f'quantum={float(frame.time_base):.6f}')
            frame_start = clock_position
            clock_position += frame.samples
            t = frame_start / input_rate
            if t - video_origin >= end:
                break
            a = max(0, int(np.ceil((video_origin + start - t) * frame.sample_rate - 1e-7)))
            b = min(frame.samples, int(np.ceil((video_origin + end - t) * frame.sample_rate - 1e-7)))
            if b <= a:
                continue
            raw = frame.to_ndarray()
            cropped = raw[:, a:b] if frame.format.is_planar else raw[:, a * channels:b * channels]
            normalized = av.AudioFrame.from_ndarray(np.ascontiguousarray(cropped),
                                                     format=frame.format.name, layout=frame.layout.name)
            normalized.sample_rate = frame.sample_rate
            normalized.time_base = Fraction(1, frame.sample_rate)
            normalized.pts = frame_start + a
            for converted in resampler.resample(normalized):
                store(converted)
        for converted in resampler.resample(None):
            store(converted)
    result = waveform[:, 0] if channels == 1 else waveform
    missing = np.flatnonzero(~np.isfinite(waveform).all(axis=1))
    if len(missing):
        raise ValueError(f'Audio coverage missing {len(missing)} sample positions; '
                         f'first={missing[:5].tolist()} last={missing[-5:].tolist()}')
    validate_audio(result)
    return result
