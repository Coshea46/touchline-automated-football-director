"""Offline tests for unit ml_inference features test_audio_contracts."""


import hashlib
import json
from fractions import Fraction
import numpy as np
import pytest
from touchline.ml_inference.features.audio import DECODER_SHA256, LEGACY_AUDIO, SAMPLECLOCK_AUDIO, validate_aligned_pcm, verify_reference_decoder
from touchline.ml_inference.features.media import MediaTimelineError
from tests.support.ml_inference.observations import Detector, append_range, stream
from tests.support.ml_inference.media import AV, collect, cursor
from tests.paths import ML_FIXTURES


FIXTURES = ML_FIXTURES / 'audio_v3'


def test_exact_decoder_and_released_pcm_raw_fixture_provenance():
    assert hashlib.sha256(verify_reference_decoder().read_bytes()).hexdigest() == DECODER_SHA256
    metadata = json.loads((FIXTURES / 'provenance.json').read_text())
    for filename, key in [('pcm.npy', 'pcm_file_sha256'), ('raw.npy', 'raw_file_sha256')]:
        assert hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest() == metadata[key]
    assert metadata['split'] == 'valid' and metadata['test_accessed'] is False


def test_vorbis_three_tick_jitter_is_corrected_without_changing_native_waveform():
    av = AV()
    av.audio_stream.time_base = av.audio[0].time_base = Fraction(1, 1000)
    av.audio_stream.codec_context.name = 'vorbis'
    for i, frame in enumerate(av.audio):
        frame.time_base = av.audio_stream.time_base
        frame.pts = round(frame.pts / 16) + (3 if i % 2 else 0)
    worker, _ = cursor(av, audio_contract=SAMPLECLOCK_AUDIO)
    collect(worker, 5)
    reference, _ = cursor()
    collect(reference, 5)
    np.testing.assert_array_equal(worker.audio_window(0, 5), reference.audio_window(0, 5))
    assert worker.stats['audio_clock_corrections'] == 5
    assert worker.stats['audio_max_clock_deviation_seconds'] == pytest.approx(.003)
    assert worker.stats['audio_real_discontinuities'] == 0


def test_genuine_missing_packets_remain_nan_not_silence_or_source_failure():
    av = AV()
    for frame in av.audio[4:]:
        frame.pts += 1600
    worker, _ = cursor(av, audio_contract=SAMPLECLOCK_AUDIO)
    collect(worker, 7)
    audio = worker.audio_window(0, 5)
    assert np.isnan(audio[32000:33600]).all()
    assert np.isfinite(audio[:32000]).all() and np.isfinite(audio[33600:]).all()
    assert worker.stats['audio_real_discontinuities'] == 1
    assert worker.stats['audio_clock_corrections'] == 0
    assert worker.stats['audio_recent_discontinuities'][0]['gap_seconds'] == pytest.approx(.1)
    assert all(frame.pts + frame.samples <= 80000 for frame in av.resamplers[0].frames)
    assert np.isnan(worker.audio_window(2, 7)[:1600]).all()
    legacy_av = AV()
    legacy_av.audio[4].pts += 1600
    legacy, _ = cursor(legacy_av, audio_contract=LEGACY_AUDIO)
    with pytest.raises(MediaTimelineError, match='discontinuity'):
        collect(legacy, 5)


def test_non_vorbis_never_inherits_vorbis_three_tick_envelope():
    av = AV()
    av.audio_stream.codec_context.name = 'aac'
    av.audio_stream.time_base = Fraction(1, 1000)
    for i, frame in enumerate(av.audio):
        frame.time_base = Fraction(1, 1000)
        frame.pts = round(frame.pts / 16) + (2 if i else 0)
    worker, _ = cursor(av, audio_contract=SAMPLECLOCK_AUDIO)
    collect(worker, 5)
    assert worker.stats['audio_real_discontinuities'] == 1
    assert np.isnan(worker.audio_window(0, 5)[8000:8032]).all()


def test_large_real_gap_lookahead_cannot_evict_permitted_pre_gap_audio():
    av = AV()
    for frame in av.audio[4:]:
        frame.pts += 30 * 16000
    worker, _ = cursor(av, audio_contract=SAMPLECLOCK_AUDIO)
    collect(worker, 5)
    audio = worker.audio_window(0, 5)
    assert np.isfinite(audio[:32000]).all() and np.isnan(audio[32000:]).all()
    assert worker.stats['audio_real_discontinuities'] == 1
    assert all(frame.pts + frame.samples <= 80000 for frame in av.resamplers[0].frames)


def test_real_gap_support_does_not_disguise_evicted_history_or_future_samples():
    worker, _ = cursor(AV(seconds=30), audio_contract=SAMPLECLOCK_AUDIO)
    collect(worker, 25)
    with pytest.raises(MediaTimelineError, match='evicted'):
        worker.audio_window(0, 5)
    with pytest.raises(MediaTimelineError, match='permitted'):
        worker.audio_window(21, 26)
    assert worker.stats['audio_history_bytes'] <= 12 * 16000 * 2 * 4


def test_v3_missing_stream_and_audio_eof_remain_explicit_without_stopping_video():
    av = AV()
    av.audio = av.audio[:2]
    worker, _ = cursor(av, audio_contract=SAMPLECLOCK_AUDIO)
    assert len(collect(worker, 5)) == 75
    audio = worker.audio_window(0, 5)
    assert np.isfinite(audio[:16000]).all() and np.isnan(audio[16000:]).all()
    no_audio = AV()
    original = no_audio.open

    def open_without_audio(*args, **kwargs):
        container = original(*args, **kwargs)
        container.streams.audio = []
        return container

    no_audio.open = open_without_audio
    absent, _ = cursor(no_audio, audio_contract=SAMPLECLOCK_AUDIO)
    assert len(collect(absent, 5)) == 75 and absent.audio_window(0, 5) is None
    assert absent.metadata['has_audio'] is False


@pytest.mark.parametrize('bad', [np.zeros(79999, np.float32), np.zeros(80000, np.float64),
                               np.full(80000, np.inf, np.float32), np.full(80000, 1.01, np.float32)])
def test_v3_admission_rejects_malformed_pcm_but_preserves_nan_gaps(bad):
    validate_aligned_pcm(np.full(80000, np.nan, np.float32))
    validate_aligned_pcm(None)
    with pytest.raises((ValueError, TypeError)):
        validate_aligned_pcm(bad)


def test_v3_incremental_repairs_once_with_original_source_interval_no_yolo_repeat():
    calls = []

    def repair(raw, audio, *, endpoint_seconds):
        assert np.isnan(audio[100:200]).all()
        calls.append((raw.copy(), endpoint_seconds))
        output = raw.copy()
        output[:, 210:] = [-.2, 0, .1, .3, 1]
        output[3:7, 210:] = 0
        return output

    detector = Detector()
    worker = stream(detector, audio_contract=SAMPLECLOCK_AUDIO, core_audio_repair_fn=repair,
                    timeline_origin_seconds=120.017)
    audio = np.zeros((80000, 2), np.float32)
    audio[100:200] = np.nan
    original_audio = audio.copy()
    for start, end in [(0, 75), (75, 105), (105, 135)]:
        append_range(worker, start, end)
        features = worker.assemble(end, audio)
        assert features[:, :210].tobytes() == calls[-1][0][:, :210].tobytes()
        assert calls[-1][1] == pytest.approx(120.017 + end / 15)
        assert worker.last_result.diagnostics['audio_available_intervals'] == 71
        assert worker.last_result.diagnostics['audio_decoder_sha256'] == DECODER_SHA256
    np.testing.assert_array_equal(audio, original_audio)
    assert len(calls) == 3 and detector.indexes == list(range(135))
    assert worker.stats['detector_cache_hits'] == 90
    assert worker.stats['windows_assembled'] == 3
