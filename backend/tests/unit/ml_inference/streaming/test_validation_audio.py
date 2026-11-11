"""Offline tests for unit ml_inference streaming test_validation_audio."""


import sys
from types import ModuleType, SimpleNamespace
import numpy as np
import pytest
from touchline.ml_inference.features import incremental, media as replay_media
from touchline.ml_inference.features.audio import DECODER_SHA256
from touchline.ml_inference.streaming.validation import validate_direct_sources


def fixture(monkeypatch, *, mismatch=False, error=False):
    calls = {'cursor': [], 'features': [], 'reference': [], 'audio': []}
    audio = np.full((80000, 2), .2, np.float32)
    audio[8000:9600] = np.nan
    features = np.zeros((75, 215), np.float32)
    features[:, 214] = 1
    features[7, 214] = 0
    diagnostics = {'slot_ids': [0], 'player_interpolated': [False], 'ball_interpolated': [False],
                   'detector_frames_inferred': 30, 'detector_cache_hits': 45}

    class Features:
        def __init__(self, *args, **kwargs):
            calls['features'].append(kwargs)
            self.stats = {}
            self.last_result = SimpleNamespace(diagnostics=diagnostics, timings={})

        def append_batch(self, *args):
            pass

        def assemble(self, index, sound):
            assert np.isnan(sound).any()
            return features.copy()

    class Cursor:
        def __init__(self, *args, **kwargs):
            calls['cursor'].append(kwargs)
            self.next_index = 0
            self.metadata, self.stats = {'video_origin_seconds': .04}, {}
            self.closed = False

        def pull(self, permit, count):
            start = self.next_index
            self.next_index += count
            return [SimpleNamespace(sample_index=i, rgb=None, source_timestamp=i / 15)
                    for i in range(start, self.next_index)]

        def audio_window(self, start, end):
            return audio.copy()

        def close(self):
            self.closed = True

    def reference(*args, **kwargs):
        calls['reference'].append(kwargs)
        return SimpleNamespace(features=features.copy(), diagnostics=diagnostics, timings={})

    def decode(source, **kwargs):
        calls['audio'].append(kwargs)
        if error:
            raise OSError(source)
        result = audio.copy()
        if mismatch:
            result[8000:9600] = .2
        return SimpleNamespace(pcm=result)

    monkeypatch.setattr(incremental, 'IncrementalMatchFeatures', Features)
    monkeypatch.setattr(incremental, 'fresh_reference_window', reference)
    monkeypatch.setattr(replay_media, 'MediaCursor', Cursor)
    core = ModuleType('matchpulse.research.core_audio')
    core.decode_aligned_audio = decode
    monkeypatch.setitem(sys.modules, 'matchpulse.research.core_audio', core)
    monkeypatch.setitem(sys.modules, 'av', SimpleNamespace())
    spec = {'session_id': 'audio-proof', 'mode': 'features_mock', 'model_version': 'features-mock-v1',
            'max_duration_seconds': 30, 'max_estimated_cost_usd': 1,
            'matches': [{'match_id': 'cam-1', 'source_url': 'https://private.test/media?token=secret',
                         'offset_seconds': 120}]}
    return spec, calls


def test_v3_equivalence_checks_independent_original_source_pcm_without_losing_nan_gaps(monkeypatch):
    spec, calls = fixture(monkeypatch)
    report = validate_direct_sources(spec, None, None, {}, audio_contract='sampleclock-v3')
    assert report['passed'] and len(report['windows']) == 3
    assert calls['cursor'][0]['audio_contract'] == 'sampleclock-v3'
    assert calls['features'][0]['audio_contract'] == 'sampleclock-v3'
    assert [c['end_seconds'] for c in calls['audio']] == [125, 127, 129]
    assert all(c['video_origin_seconds'] == .04 for c in calls['audio'])
    assert all(c['audio_contract'] == 'sampleclock-v3' for c in calls['reference'])
    for record in report['windows']:
        assert record['audio_pcm_matches_frozen_decoder'] and record['audio_pcm_availability_exact']
        assert record['audio_availability_exact'] and record['audio_core5_close']
        assert record['audio_available_intervals'] == 74
        assert record['audio_rms'] == pytest.approx(.2)
        assert record['audio_pcm_finite_fraction'] == pytest.approx(.98)
        assert record['audio_decoder_sha256'] == DECODER_SHA256


def test_v3_independent_availability_mismatch_fails_even_when_fake_feature_values_match(monkeypatch):
    spec, _ = fixture(monkeypatch, mismatch=True)
    report = validate_direct_sources(spec, None, None, {}, audio_contract='sampleclock-v3')
    assert not report['passed'] and len(report['windows']) == 1
    record = report['windows'][0]
    assert record['audio_equal'] and not record['audio_pcm_availability_exact']
    assert not record['audio_pcm_matches_frozen_decoder']


def test_v3_frozen_reference_exception_never_discloses_private_url(monkeypatch):
    spec, _ = fixture(monkeypatch, error=True)
    with pytest.raises(RuntimeError, match='Frozen reference audio decode failed') as raised:
        validate_direct_sources(spec, None, None, {}, audio_contract='sampleclock-v3')
    assert 'secret' not in str(raised.value) and 'https://' not in str(raised.value)
