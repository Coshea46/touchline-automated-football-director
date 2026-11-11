"""Offline tests for unit ml_inference features test_media_audio."""


from fractions import Fraction
import numpy as np
import pytest
from touchline.ml_inference.features.media import MediaTimelineError, _native_pcm
from tests.support.ml_inference.media import AudioFrame, AV, cursor, collect


def test_priming_does_not_change_successive_sample_pixels_pts_or_window_audio():
    primed, _ = cursor()
    plain, _ = cursor()
    primed.prime(0)
    a, b = collect(primed, 5), collect(plain, 5)
    assert [s.sample_index for s in a] == [s.sample_index for s in b]
    assert [s.source_timestamp for s in a] == [s.source_timestamp for s in b]
    assert all(np.array_equal(x.rgb, y.rgb) for x, y in zip(a, b, strict=True))
    np.testing.assert_array_equal(primed.audio_window(0, 5), plain.audio_window(0, 5))


@pytest.mark.parametrize('channels', [1, 2])
def test_native_pcm_is_cropped_before_resampling_and_stereo_does_not_cancel(channels):
    worker, av = cursor(AV(channels=channels))
    collect(worker, 7)
    result = worker.audio_window(0, 5)
    assert result.shape == ((80000,) if channels == 1 else (80000, 2))
    assert result.dtype == np.float32 and np.isfinite(result).all()
    if channels == 2:
        np.testing.assert_array_equal(result[:, 1], -result[:, 0])
        assert np.mean(result ** 2) > 0
    assert len(av.resamplers) == 1 and av.flushes == 1
    for frame in av.resamplers[0].frames:
        assert frame.pts >= 0
        assert frame.pts + frame.samples <= 5 * 16000
    second = worker.audio_window(2, 7)
    assert second.shape == result.shape
    assert av.flushes == 2  # Independent FIR boundary for each completed window.


def test_audio_is_available_at_exact_window_end_even_without_new_video_samples():
    worker, _ = cursor()
    collect(worker, 5 - 1 / 60)
    assert worker.next_index == 75
    assert worker.pull(5) == []
    assert worker.audio_window(0, 5).shape == (80000, 2)


def test_native_history_is_bounded_and_evicted_windows_fail_instead_of_padding():
    worker, _ = cursor(AV(seconds=30), max_history_seconds=12)
    collect(worker, 25)
    assert worker.stats['audio_history_bytes'] <= 12 * 16000 * 2 * 4
    with pytest.raises(MediaTimelineError, match='coverage|evicted'):
        worker.audio_window(0, 5)
    assert worker.audio_window(20, 25).shape == (80000, 2)


def test_audio_timestamp_quantization_uses_exact_sample_clock():
    av = AV()
    for frame in av.audio:
        frame.pts = round(frame.pts / 16)
        frame.time_base = Fraction(1, 1000)
    worker, _ = cursor(av)
    collect(worker, 5)
    assert np.isfinite(worker.audio_window(0, 5)).all()


def test_remuxed_fine_timebase_does_not_erase_bounded_native_clock_quantization():
    av = AV()
    for i, frame in enumerate(av.audio):
        frame.pts += (i % 3 - 1) * 10  # <=1.25ms relative to the initial anchor.
    worker, _ = cursor(av)
    collect(worker, 5)
    assert np.isfinite(worker.audio_window(0, 5)).all()
    assert worker.stats['audio_max_clock_deviation_seconds'] <= 0.002
    strict_av = AV()
    for i, frame in enumerate(strict_av.audio):
        frame.pts += (i % 3 - 1) * 10
    strict, _ = cursor(strict_av, audio_clock_tolerance_seconds=0)
    with pytest.raises(MediaTimelineError, match='discontinuity'):
        strict.pull(5)


def test_clock_policy_never_bridges_a_real_gap_or_cumulative_drift():
    av = AV()
    av.audio[4].pts += 33  # >2ms with a perfectly exact1/16000 time base.
    worker, _ = cursor(av)
    with pytest.raises(MediaTimelineError, match='discontinuity'):
        worker.pull(5)


def test_buffered_audio_lookahead_cannot_be_used_beyond_the_permit():
    worker, _ = cursor()
    worker.pull(5.01)
    with pytest.raises(MediaTimelineError, match='permitted'):
        worker.audio_window(0.02, 5.02)


def test_packed_integer_stereo_preserves_channel_order_and_full_scale():
    packed = np.array([[-32768, 32767, 16384, -16384]], np.int16)
    decoded = _native_pcm(AudioFrame(packed, planar=False))
    assert decoded.dtype == np.float32
    np.testing.assert_array_equal(decoded, [[-1, 32767 / 32768], [0.5, -0.5]])


@pytest.mark.parametrize('kind', ['gap', 'missing', 'rate', 'channels', 'nonfinite'])
def test_audio_discontinuity_never_becomes_silence(kind):
    av = AV()
    if kind == 'gap':
        av.audio[3].pts += 1600
    elif kind == 'missing':
        av.audio[3].pts = None
    elif kind == 'rate':
        av.audio[3].sample_rate = 48000
    elif kind == 'channels':
        av.audio[3] = AudioFrame(np.zeros((1, 8000), np.float32), pts=24000)
    else:
        av.audio[3]._data[0, 0] = np.nan
    worker, _ = cursor(av)
    with pytest.raises(MediaTimelineError):
        worker.pull(5)
    with pytest.raises(RuntimeError, match='failed'):
        worker.pull(5)


def test_oversized_metadata_is_rejected_before_audio_open_or_rgb_allocation():
    av = AV()
    av.video_stream.codec_context.width = 3840
    av.video_stream.codec_context.height = 2160
    with pytest.raises(MediaTimelineError, match='pixel frame limit'):
        cursor(av)
    assert len(av.containers) == 1 and av.containers[0].closed
    assert all(frame.converted == 0 for frame in av.video)


def test_pause_rewind_close_and_invalid_audio_window_contracts():
    worker, av = cursor()
    worker.pull(1)
    with pytest.raises(ValueError, match='backwards'):
        worker.pull(0.5)
    with pytest.raises(ValueError, match='five seconds'):
        worker.audio_window(0, 4)
    worker.close()
    worker.close()
    assert all(c.closed for c in av.containers)
    assert worker.stats['audio_history_bytes'] == 0
    with pytest.raises(RuntimeError, match='closed'):
        worker.pull(1)
