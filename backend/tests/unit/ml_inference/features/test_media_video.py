"""Offline tests for unit ml_inference features test_media_video."""


from fractions import Fraction
import numpy as np
import pytest
from touchline.ml_inference.features.media import HTTP_OPTIONS
from tests.support.ml_inference.media import VideoFrame, AV, cursor, collect, PRIVATE_SOURCE


def test_persistent_latest_before_centre_sampling_reads_each_selected_rgb_once():
    worker, av = cursor()
    samples = collect(worker, 5)
    assert [s.sample_index for s in samples] == list(range(75))
    expected = [int(np.floor((i + 0.5) * 25 / 15 + 1e-9)) for i in range(75)]
    assert [int(s.rgb[0, 0, 0]) for s in samples] == expected
    np.testing.assert_allclose([s.source_timestamp for s in samples], np.array(expected) / 25)
    assert len(av.containers) == 2
    assert [len(c.seek_calls) for c in av.containers] == [1, 1]
    assert all(f.converted == int(f.index in expected) for f in av.video)
    assert all(f.converted == 0 for f in av.video if f.index / 25 >= 5)
    assert worker.stats['rgb_history_frames'] == 1
    assert worker.metadata['audio_channels'] == 2
    assert worker.pull(5) == []


def test_prime_uses_exact_current_frame_without_sampling_or_future_rgb_at_60fps():
    av = AV()
    av.video = [VideoFrame(i) for i in range(600)]
    av.video_stream.time_base, av.video_stream.average_rate = Fraction(1, 60), 60
    av.video_stream.duration = 600
    worker, _ = cursor(av)
    worker.prime(0)
    assert worker.next_index == 0 and worker._last_permit is None
    assert worker.stats['primed']
    assert [f.index for f in av.video if f.converted] == [0]
    assert worker._video_lookahead.timestamp == pytest.approx(1 / 60)
    assert worker.stats['audio_history_bytes'] > 0
    first = worker.pull(1 / 30)
    assert len(first) == 1 and first[0].sample_index == 0
    assert first[0].source_timestamp == pytest.approx(2 / 60)


def test_future_base_cannot_be_primed_and_http_reconnect_is_bounded_without_eof_loop():
    worker, av = cursor(start_index=30)
    with pytest.raises(ValueError, match='future'):
        worker.prime(1.9)
    assert worker.next_index == 30 and not any(frame.converted for frame in av.video)
    assert worker.stats['audio_history_bytes'] == 0
    assert HTTP_OPTIONS['reconnect_max_retries'] == '2'
    assert HTTP_OPTIONS['reconnect_delay_max'] == '1'
    assert HTTP_OPTIONS['rw_timeout'] == '1000000'
    assert HTTP_OPTIONS['reconnect_on_http_error'] == '429,5xx'
    assert 'reconnect_at_eof' not in HTTP_OPTIONS
    assert av.video_stream.codec_context.thread_count == 1
    assert av.audio_stream.codec_context.thread_count == 1


def test_leaf_av_failure_class_and_phase_are_sanitized_and_retained_in_stats():
    worker, _ = cursor()

    def broken():
        raise OSError(PRIVATE_SOURCE)
        yield  # pragma: no cover

    worker._video_frames = broken()
    with pytest.raises(RuntimeError) as raised:
        worker.prime(0)
    assert worker.stats['last_av_error_type'] == 'OSError'
    assert worker.stats['last_av_error_phase'] == 'video_decode'
    assert raised.value.media_error_type == 'OSError'
    assert raised.value.media_error_phase == 'video_decode'
    assert 'secret-test-token' not in str(raised.value) + str(worker.stats)


def test_nonzero_stream_origin_and_replay_offset_preserve_source_timeline():
    worker, av = cursor(AV(seconds=12, origin=10), offset_seconds=2, start_index=30)
    samples = collect(worker, 7)
    assert samples[0].sample_index == 30
    assert samples[0].source_timestamp == pytest.approx(4)
    result = worker.audio_window(2, 7)
    assert result[0, 0] == pytest.approx(0.08)
    assert worker._video_container.seek_calls[0][0] == int((10 + 2 + 2 - 0.25) * 25)


def test_partial_final_batch_is_returned_before_typed_eof():
    worker, _ = cursor(AV(seconds=1))
    samples = worker.pull(2, limit=16)
    assert [s.sample_index for s in samples] == list(range(15))
    assert worker.eof
    with pytest.raises(EOFError):
        worker.pull(2)
