"""Offline tests for unit ml_inference features test_incremental."""


import weakref
from types import SimpleNamespace
import numpy as np
import pytest
from touchline.ml_inference.features.incremental import fresh_reference_window
from tests.support.ml_inference.observations import Detector, sound_features, Extractor, stream, frame, pts, append_range, fresh_reference


@pytest.mark.parametrize('origin', [0, 120.017])
@pytest.mark.parametrize('external', [False, True])
def test_overlapping_completed_windows_match_fresh_with_exact_source_pts(origin, external):
    detector = Detector()
    worker = stream(detector, timeline_origin_seconds=origin)
    mono = np.sin(np.arange(80000) * 0.1).astype(np.float32) * 0.2
    audio = np.column_stack([mono, -mono])
    previous = 0
    for n, end in enumerate((75, 105, 135)):
        append_range(worker, previous, end, external=external)
        actual = worker.assemble(end, audio)
        np.testing.assert_allclose(actual, fresh_reference(end - 75, end, audio, origin),
                                   atol=2e-5, rtol=1e-5)
        assert actual.shape == (75, 215) and actual.dtype == np.float32
        assert np.all(actual[:, 214] == 1)
        assert actual[0, 208] == 0  # Cached cut before this window is overridden.
        assert actual[0, 210] > 0  # Stereo energy did not cancel.
        diagnostics = worker.last_result.diagnostics
        assert diagnostics['detector_frames_inferred'] == (75 if n == 0 else 30)
        assert diagnostics['detector_cache_hits'] == (0 if n == 0 else 45)
        assert diagnostics['source_end_seconds'] == origin + end / 15
        assert worker.last_result.timings['camera'] == pytest.approx((75 if n == 0 else 30) * 0.02)
        assert worker.stats['cache_samples'] == 75
        assert worker.stats['oldest_sample_index'] == end - 75
        previous = end
    assert worker.stats['samples_seen'] == worker.stats['detector_frames_inferred'] == 135
    assert worker.stats['detector_cache_hits'] == 90
    assert worker.stats['windows_assembled'] == 3
    assert worker._extractor.calls == 3
    if external:
        assert detector.calls == 0
        assert worker.stats['detector_frames_supplied'] == 135
    else:
        assert detector.indexes == list(range(135))
        assert detector.calls == 3


def test_rgb_is_released_and_cache_stays_bounded_over_long_replay():
    worker = stream()
    for i in range(450):
        pixels = frame(i)
        reference = weakref.ref(pixels)
        worker.append(i, pixels, pts(i))
        del pixels
        assert reference() is None
    assert worker.stats['cache_samples'] == 75
    assert worker.stats['oldest_sample_index'] == 375
    assert worker.stats['cache_rgb_bytes'] == 0
    assert worker.stats['cache_bytes'] == 75 * 4 * 6


def test_match_and_epoch_state_never_mix_even_with_a_shared_detector():
    detector = Detector()
    a, b = stream(detector), stream(detector)
    a.append(0, frame(1), pts(0))
    b.append(0, frame(2), pts(0))
    assert a._observations[0][0].detections == (1,)
    assert b._observations[0][0].detections == (2,)
    a.reset_epoch('seek-1', timeline_origin_seconds=60)
    a.append(0, frame(3), pts(0, 60))
    assert a.stats['source_epoch'] == 'seek-1'
    assert a.stats['samples_seen'] == 1
    assert a._observations[0][0].camera.dx == 0
    assert b.stats['samples_seen'] == 1
    assert detector.indexes == [1, 2, 3]
    with pytest.raises(ValueError, match='new source_epoch'):
        a.reset_epoch('seek-1')


def test_seek_can_start_on_nonzero_replay_index():
    worker = stream(timeline_origin_seconds=5)
    append_range(worker, 1800, 1875)
    worker.assemble(1875, np.zeros(80000, np.float32))
    assert worker.last_result.diagnostics['window_start_seconds'] == 120
    assert worker.last_result.diagnostics['source_start_seconds'] == 125


@pytest.mark.parametrize('indexes,times', [
    ([0, 2], [0, 0.1]), ([0, 1], [0, -0.01]), ([0], [1]), ([0], [-1]),
    ([True], [0]), ([-1], [0]), ([0], [np.nan]), ([0], [np.inf]),
])
def test_bad_grid_or_source_time_rejected_before_detection(indexes, times):
    detector = Detector()
    worker = stream(detector)
    with pytest.raises((ValueError, TypeError)):
        worker.append_batch(indexes, [frame(0) for _ in indexes], times)
    assert detector.calls == 0 and worker.stats['samples_seen'] == 0


def test_duplicate_sample_and_resolution_change_do_not_repeat_detection():
    detector = Detector()
    worker = stream(detector)
    worker.append(0, frame(0), pts(0))
    with pytest.raises(ValueError, match='consecutive new'):
        worker.append(0, frame(0), pts(0))
    with pytest.raises(ValueError, match='dimensions changed'):
        worker.append(1, np.zeros((32, 24, 3), np.uint8), pts(1))
    assert detector.indexes == [0]


def test_incomplete_future_or_wrong_stride_windows_fail():
    worker = stream()
    audio = np.zeros(80000, np.float32)
    append_range(worker, 0, 74)
    with pytest.raises(ValueError, match='complete 75'):
        worker.assemble(75, audio)
    worker.append(74, frame(74), pts(74))
    worker.assemble(75, audio)
    with pytest.raises(ValueError, match='exactly 30'):
        worker.assemble(76, audio)
    append_range(worker, 75, 106)
    with pytest.raises(ValueError, match='no future'):
        worker.assemble(105, audio)


def test_missing_audio_rejected_in_full_feature_path():
    worker = stream()
    append_range(worker, 0, 75)
    with pytest.raises(TypeError, match='NumPy'):
        worker.assemble(75, None)
    with pytest.raises(ValueError, match='exactly five'):
        worker.assemble(75, np.zeros(79999, np.float32))


def test_failed_analysis_requires_a_new_epoch():
    worker = stream(max_cache_bytes=1)
    with pytest.raises(MemoryError):
        worker.append(0, frame(0), pts(0))
    assert worker.stats['failed']
    assert worker.stats['cache_bytes'] == 0
    with pytest.raises(RuntimeError, match='reset'):
        worker.append(0, frame(0), pts(0))


def test_fresh_reference_repeats_detection_and_preserves_actual_source_times():
    class ReferenceExtractor(Extractor):
        def _assemble(self, frames, raw, times, timing):
            assert len(frames) == 75 and len(raw) == 75
            np.testing.assert_allclose(times, [pts(i, 120) - 120 for i in range(75)])
            assert raw == [(i,) for i in range(75)]
            result = np.zeros((75, 215), np.float32)
            return result, {'source_timestamps': times}

    detector = Detector()
    result = fresh_reference_window(
        detector, [frame(i) for i in range(75)], [pts(i, 120) for i in range(75)],
        np.zeros(80000, np.float32), window_start_seconds=120,
        config=SimpleNamespace(clip_duration_seconds=5),
        extractor_factory=ReferenceExtractor, audio_features_fn=sound_features,
    )
    assert detector.indexes == list(range(75))
    assert result.diagnostics['detector_frames_inferred'] == 75
    assert np.all(result.features[:, 214] == 1)
