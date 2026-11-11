"""Bounded direct-media overlap proof, run only by the private inference worker."""

import time
from collections import deque
from contextlib import nullcontext

from touchline.ml_inference.features.audio import (
    DECODER_SHA256,
    LEGACY_AUDIO,
    SAMPLECLOCK_AUDIO,
    validate_audio_contract,
    verify_reference_decoder,
)


def validate_direct_sources(spec, detector, detector_config, runtime, *, audio_contract=LEGACY_AUDIO):
    import numpy as np

    from touchline.ml_inference.features.incremental import IncrementalMatchFeatures, fresh_reference_window
    from touchline.ml_inference.streaming.contract import validate_session
    from touchline.ml_inference.features.media import MediaCursor

    validate_session(spec)
    validate_audio_contract(audio_contract)
    audio_options = {} if audio_contract == LEGACY_AUDIO else {'audio_contract': audio_contract}
    if len(spec['matches']) > 2:
        raise ValueError('Equivalence proof is bounded to two real sources')
    started = time.perf_counter()
    deadline = started + 180
    results = []
    for source in spec['matches']:
        key, offset = source['match_id'], float(source.get('offset_seconds', 0))
        pipeline = IncrementalMatchFeatures(key, detector, detector_config=detector_config,
                                           timeline_origin_seconds=offset, **audio_options)
        cursor = MediaCursor(source['source_url'], offset_seconds=offset, **audio_options)
        history = deque(maxlen=75)
        try:
            for end_index in (75, 105, 135):
                while cursor.next_index < end_index:
                    if time.perf_counter() >= deadline:
                        raise TimeoutError('Bounded direct-media validation deadline reached')
                    samples = cursor.pull(end_index / 15, min(16, end_index - cursor.next_index))
                    if not samples:
                        raise RuntimeError('Source did not supply the completed window')
                    pipeline.append_batch([s.sample_index for s in samples],
                                          [s.rgb for s in samples], [s.source_timestamp for s in samples])
                    history.extend(samples)
                window_start, window_end = (end_index - 75) / 15, end_index / 15
                audio = cursor.audio_window(window_start, window_end)
                finite_pcm = (audio[np.isfinite(audio)].astype(np.float64)
                              if audio is not None else np.empty(0))
                rms = float(np.sqrt(np.mean(finite_pcm ** 2))) if finite_pcm.size else 0.0
                if rms < 1e-5:
                    raise ValueError('Full-audio proof requires non-silent real source audio')
                reference_audio, audio_pcm_equal, pcm_mask_equal = audio, True, True
                if audio_contract == SAMPLECLOCK_AUDIO:
                    # Validation alone reopens the exact frozen decoder. The
                    # production reader remains persistent and never reruns YOLO.
                    import av
                    from matchpulse.research.core_audio import decode_aligned_audio

                    capture = getattr(getattr(av, 'logging', None), 'Capture', None)
                    try:
                        with capture(local=True) if capture else nullcontext():
                            reference_audio = decode_aligned_audio(
                                source['source_url'], start_seconds=offset + window_start,
                                end_seconds=offset + window_end,
                                video_origin_seconds=cursor.metadata['video_origin_seconds'],
                                decoder_path=verify_reference_decoder()).pcm
                    except Exception as exc:  # noqa: BLE001 -- decoder errors may contain private URLs.
                        raise RuntimeError(f'Frozen reference audio decode failed ({type(exc).__name__})') from None
                    pcm_mask_equal = bool(reference_audio is not None and
                        np.array_equal(np.isfinite(audio), np.isfinite(reference_audio)))
                    audio_pcm_equal = bool(pcm_mask_equal and
                        np.allclose(audio, reference_audio, atol=2e-6, rtol=1e-5, equal_nan=True))
                t = time.perf_counter()
                features = pipeline.assemble(end_index, audio)
                assembly_seconds = time.perf_counter() - t
                actual = pipeline.last_result
                expected = fresh_reference_window(detector, [s.rgb for s in history],
                    [s.source_timestamp for s in history], reference_audio,
                    window_start_seconds=offset + window_start, detector_config=detector_config,
                    **audio_options)
                if time.perf_counter() >= deadline:
                    raise TimeoutError('Bounded direct-media validation deadline reached')
                close = np.isclose(features, expected.features, atol=2e-5, rtol=1e-5)
                slot_equal = np.array_equal(actual.diagnostics['slot_ids'], expected.diagnostics['slot_ids'])
                interpolation_equal = (np.array_equal(actual.diagnostics['player_interpolated'], expected.diagnostics['player_interpolated'])
                    and np.array_equal(actual.diagnostics['ball_interpolated'], expected.diagnostics['ball_interpolated']))
                record = {'match_id': key, 'observation_start': window_start,
                    'observation_end': window_end, 'source_end': offset + window_end,
                    'shape': list(features.shape), 'dtype': str(features.dtype),
                    'finite': bool(np.isfinite(features).all()), 'atol': 2e-5, 'rtol': 1e-5,
                    'max_absolute_difference': float(np.max(np.abs(features - expected.features))),
                    'elements_outside_tolerance': int((~close).sum()),
                    'player_slots_equal': bool(slot_equal), 'interpolation_equal': bool(interpolation_equal),
                    'teams_equal': bool(np.array_equal(features[:, :198].reshape(75, 22, 9)[:, :, 8],
                                                      expected.features[:, :198].reshape(75, 22, 9)[:, :, 8])),
                    'ball_equal': bool(np.allclose(features[:, 198:205], expected.features[:, 198:205], atol=2e-5, rtol=1e-5)),
                    'camera_equal': bool(np.allclose(features[:, 205:210], expected.features[:, 205:210], atol=2e-5, rtol=1e-5)),
                    'audio_equal': bool(np.array_equal(features[:, 210:215], expected.features[:, 210:215])),
                    'audio_core5_close': bool(np.allclose(features[:, 210:215], expected.features[:, 210:215],
                                                          atol=2e-6, rtol=1e-5)),
                    'audio_availability_exact': bool(np.array_equal(features[:, 214], expected.features[:, 214])),
                    'audio_pcm_matches_frozen_decoder': audio_pcm_equal,
                    'audio_pcm_availability_exact': pcm_mask_equal,
                    'audio_contract': audio_contract,
                    'audio_decoder_sha256': DECODER_SHA256 if audio_contract == SAMPLECLOCK_AUDIO else None,
                    'audio_available_intervals': int(features[:, 214].sum()),
                    'audio_pcm_finite_fraction': float(np.isfinite(audio).mean()),
                    'audio_rms': rms,
                    'new_detector_frames': actual.diagnostics['detector_frames_inferred'],
                    'detector_cache_hits': actual.diagnostics['detector_cache_hits'],
                    'incremental_timings': actual.timings, 'reference_timings': expected.timings,
                    'window_assembly_seconds': assembly_seconds,
                    'cache': pipeline.stats, 'reader': cursor.stats}
                record['passed'] = bool(close.all() and slot_equal and interpolation_equal and record['teams_equal']
                    and audio_pcm_equal and record['audio_core5_close'] and record['audio_availability_exact'])
                results.append(record)
                if not record['passed']:
                    return {'passed': False, 'runtime': runtime, 'windows': results,
                            'wall_seconds': time.perf_counter() - started}
        finally:
            cursor.close()
    return {'passed': True, 'runtime': runtime, 'windows': results,
            'wall_seconds': time.perf_counter() - started,
            'note': 'Only validation retains75RGB frames for a fresh independent reference; production caches no RGB observations'}
