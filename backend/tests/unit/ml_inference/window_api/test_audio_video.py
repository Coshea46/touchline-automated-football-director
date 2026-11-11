"""Offline tests for unit ml_inference window_api test_audio_video."""


import copy
import inspect
import zlib
from types import SimpleNamespace
import numpy as np
import pytest
from touchline.ml_inference import InferenceConfig, InferenceService, select_best_match
from touchline.ml_inference.models.classifiers import MockInterestScorer, ResearchInterestScorer
from touchline.ml_inference.features.extraction import ResearchFeatureExtractor
from touchline.ml_inference.window_api.modal_client import ModalFeatureExtractor
from tests.support.ml_inference.modal import Handle, sdk_fixture
from tests.support.ml_inference.worker import load_app
from touchline.ml_inference.window_api.transport import decode_request, encode_request, payload_bytes
from touchline.ml_inference.features.validation import validate_audio, validate_features, validate_inputs


def video(value=1):
    return np.full((75, 16, 24, 3), value, np.uint8)


def waveform(stereo=False):
    mono = (0.3 * np.sin(np.arange(80000) * 0.13)).astype(np.float32)
    return np.stack([mono, -mono], axis=1) if stereo else mono


def features():
    result = np.zeros((75, 215), np.float32)
    result[:, 210] = -0.4
    result[:, 214] = 1
    return result


def test_three_argument_contract_is_required():
    signature = inspect.signature(select_best_match)
    assert list(signature.parameters) == ['current_match_id', 'match_frames', 'match_audio']
    assert signature.parameters['match_audio'].annotation == dict[str, np.ndarray]
    with pytest.raises(TypeError):
        select_best_match('a', {'a': video()})
    assert select_best_match('a', {}, {}) == 'a'


@pytest.mark.parametrize('stereo', [False, True])
def test_public_fast_mock_preserves_inputs_and_silence_is_valid(monkeypatch, stereo):
    monkeypatch.setenv('MATCHPULSE_ML_MODE', 'mock')
    frames, audio = video(), waveform(stereo)
    before = audio.copy()
    assert select_best_match('a', {'a': frames}, {'a': audio}) == 'a'
    np.testing.assert_array_equal(audio, before)
    assert select_best_match('a', {'a': frames}, {'a': np.zeros_like(audio)}) == 'a'


@pytest.mark.parametrize('bad', [None, [], np.zeros(80000, np.float64),
    np.zeros(79999, np.float32), np.zeros(80001, np.float32),
    np.zeros(220500, np.float32), np.zeros((80000, 1), np.float32),
    np.zeros((2, 80000), np.float32), np.zeros((80000, 3), np.float32),
    np.full(80000, np.nan, np.float32), np.full(80000, np.inf, np.float32),
    np.full(80000, 1.01, np.float32), np.full(80000, -1.01, np.float32)])
def test_malformed_audio_rejected_before_cloud_or_mode(bad):
    with pytest.raises((TypeError, ValueError)):
        select_best_match('a', {'a': video()}, {'a': bad})


@pytest.mark.parametrize('audio', [None, [], {}, {'b': waveform()},
    {'a': waveform(), 'b': waveform()}])
def test_exact_match_id_correspondence(audio):
    with pytest.raises((ValueError, TypeError)):
        validate_inputs('a', {'a': video()}, audio)


@pytest.mark.parametrize('stereo', [False, True])
def test_lossless_audio_video_association_and_noncontiguous_arrays(stereo):
    audio = waveform(stereo)[::-1]
    audio[0] = -0.0
    frames = video()[:, :, ::-1]
    packet = encode_request('unique-match', frames, audio)
    key, restored_frames, restored_audio = decode_request(packet, expected_id='unique-match')
    assert key == 'unique-match'
    assert restored_audio.dtype == np.float32
    assert restored_audio.tobytes() == audio.tobytes()
    np.testing.assert_array_equal(restored_frames, frames)
    assert payload_bytes(packet) == sum(map(len, packet['video']['blocks'])) + len(packet['audio']['block'])


@pytest.mark.parametrize('kind', ['version', 'id', 'video-id', 'audio-id', 'rate', 'shape',
    'dtype', 'corrupt', 'truncated', 'trailing', 'bomb', 'oversized', 'nonfinite'])
def test_audio_transport_rejects_corruption_and_cross_match_packets(kind):
    packet = encode_request('a', video(), waveform())
    packet = copy.deepcopy(packet)
    if kind == 'version':
        packet['version'] = 'old'
    elif kind == 'id':
        packet['match_id'] = 'b'
    elif kind in ('video-id', 'audio-id'):
        packet[kind.split('-')[0]]['match_id'] = 'b'
    elif kind == 'rate':
        packet['audio']['sample_rate'] = 44100
    elif kind == 'shape':
        packet['audio']['shape'] = [80000, 1000]
    elif kind == 'dtype':
        packet['audio']['dtype'] = '<f8'
    elif kind == 'corrupt':
        packet['audio']['block'] = b'bad'
    elif kind == 'truncated':
        packet['audio']['block'] = packet['audio']['block'][:-1]
    elif kind == 'trailing':
        packet['audio']['block'] += b'trailing'
    elif kind == 'bomb':
        packet['audio']['block'] = zlib.compress(bytes(1000000))
    elif kind == 'oversized':
        packet['audio']['block'] = bytes(400000)
    else:
        packet['audio']['block'] = zlib.compress(np.full(80000, np.nan, np.float32).tobytes())
    with pytest.raises((ValueError, TypeError)):
        decode_request(packet, expected_id='a')


def test_audio_presence_flags_required_for_real_scorer_and_full_service(tmp_path):
    bad = features()
    bad[0, 214] = 0
    with pytest.raises(ValueError, match='audio_available'):
        ResearchInterestScorer(tmp_path / 'missing.pt').predict_scores({'a': bad})
    service = InferenceService(InferenceConfig(mode='real', checkpoint='classifier.pt'))
    with pytest.raises(ValueError, match='requires synchronized audio'):
        service.select_video_only('a', {'a': video()})


def test_adapter_passes_unmodified_stereo_to_existing_waveform_api(tmp_path):
    weights = tmp_path / 'weights.pt'
    weights.write_bytes(b'injected fixture')
    seen = []

    class Research:
        def extract(self, frames, *, fps, audio, audio_sample_rate):
            seen.append((frames.copy(), audio.copy(), fps, audio_sample_rate))
            return features()

    adapter = ResearchFeatureExtractor(weights, factory=lambda **kw: Research())
    audio = waveform(True)
    result = adapter.extract('a', video(), audio)
    validate_features(result, missing_audio=False)
    np.testing.assert_array_equal(seen[0][1], audio)
    assert seen[0][2:] == (15, 16000)
    validate_audio(audio)


def test_client_verifies_returned_match_id_and_audio_contract():
    seen = []

    def extract(packet):
        seen.append(decode_request(packet))
        return Handle({'match_id': packet['match_id'], 'features': features(), 'metrics': {'fixture': True}})

    sdk, _, _, _ = sdk_fixture(feature=extract)
    client = ModalFeatureExtractor(InferenceConfig(mode='features_mock'), sdk=sdk)
    assert client.extract('a', video(7), waveform(True)).shape == (75, 215)
    assert seen[0][0] == 'a'
    assert client.last_metrics['a']['audio_raw_bytes'] == 640000
    sdk, _, _, _ = sdk_fixture(feature=lambda packet: Handle({'match_id': 'wrong', 'features': features()}))
    with pytest.raises(ValueError, match='match ID'):
        ModalFeatureExtractor(InferenceConfig(mode='features_mock'), sdk=sdk).extract('a', video(), waveform())


@pytest.mark.parametrize('scores,expected', [({'a': .4, 'b': .8}, 'b'), ({'a': .5, 'b': .55}, 'a')])
def test_full_audio_public_path_scoring_and_no_cross_request_state(monkeypatch, scores, expected):
    from touchline.ml_inference.window_api import api

    seen = []

    class Extractor:
        def extract(self, match_id, frames, audio):
            seen.append((match_id, int(frames[0, 0, 0, 0]), float(audio[0])))
            return features()

    service = InferenceService(InferenceConfig(mode='features_mock'), extractor=Extractor(),
                               scorer=MockInterestScorer(scores))
    monkeypatch.setenv('MATCHPULSE_ML_MODE', 'features_mock')
    monkeypatch.setattr(api, '_service', lambda config: service)
    assert select_best_match('a', {'a': video(7), 'b': video(9)},
                             {'b': np.full(80000, -.5, np.float32), 'a': np.full(80000, .25, np.float32)}) == expected
    assert seen == [('a', 7, .25), ('b', 9, -.5)]
    seen.clear()
    select_best_match('a', {'b': video(2)}, {'b': waveform()})
    assert len(seen) == 1 and seen[0][:2] == ('b', 2)


def test_full_audio_worker_decodes_and_returns_identified_features(monkeypatch):
    module, _, _ = load_app(monkeypatch)
    monkeypatch.setattr(module, 'detector_runtime', lambda *a: {'detector_device': 'cuda:0'})
    seen = []

    class Extractor:
        _extractor = SimpleNamespace(detector=object(), model_load_seconds=.1)
        last_result = SimpleNamespace(timings={'audio': .001})

        def clear_cache(self):
            pass

        def extract(self, key, frames, audio):
            seen.append((key, frames.copy(), audio.copy()))
            return features()

    worker = module.FeatureWorker()
    worker.extractor, worker.calls = Extractor(), 0
    worker.runtime, worker.initialization_seconds = {'gpu': 'T4'}, .2
    audio = waveform(True)
    result = worker.extract(encode_request('b', video(8), audio))
    assert result['match_id'] == 'b' and result['features'].shape == (75, 215)
    np.testing.assert_array_equal(seen[0][2], audio)
    assert seen[0][0] == 'b' and result['metrics']['stage_seconds']['audio'] == .001


@pytest.mark.parametrize('all_failed', [False, True])
def test_full_audio_worker_failures_keep_documented_no_prediction_policy(all_failed, caplog):
    class Extractor:
        def extract(self, key, frames, audio):
            if key == 'a' or all_failed:
                raise RuntimeError('Controlled worker failure')
            return features()

    service = InferenceService(InferenceConfig(mode='features_mock'), extractor=Extractor())
    result = service.select('a', {'a': video(), 'b': video()}, {'a': waveform(), 'b': waveform(True)})
    assert result == ('a' if all_failed else 'b')
    if all_failed:
        assert 'without a prediction' in caplog.text
