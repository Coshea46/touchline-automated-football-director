"""Offline tests for unit ml_inference streaming test_selected_replay_runner."""


from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
from touchline.ml_inference.models import classifiers
from touchline.ml_inference.models.release import LEGACY_MODEL_VERSION, NEW_MODEL_VERSION
from tests.support.ml_inference.runner import Harness
from tests.support.ml_inference.worker import load_app


def _selected_harness(monkeypatch):
    harness = Harness(monkeypatch)
    harness.spec.update(mode='real', model_version=NEW_MODEL_VERSION, checkpoint=NEW_MODEL_VERSION)
    offsets = {'match-0': 120.017, 'match-1': 260.125}
    for source in harness.spec['matches']:
        source['offset_seconds'] = offsets[source['match_id']]
    original_cursor, original_features = harness.cursor_factory, harness.feature_factory

    class Cursor(original_cursor):
        def __init__(self, url, *, offset_seconds, start_index, audio_contract):
            assert audio_contract == 'sampleclock-v3'
            super().__init__(url, offset_seconds=offset_seconds, start_index=start_index)

        def audio_window(self, start, end):
            result = super().audio_window(start, end)
            result[:] = .1 * (self.key + 1)
            result[1000 * (self.key + 1):1000 * (self.key + 1) + 500] = np.nan
            return result

    class Features(original_features):
        def __init__(self, key, detector, *, audio_contract, **kwargs):
            assert audio_contract == 'sampleclock-v3'
            super().__init__(key, detector, **kwargs)

        def assemble(self, end, audio):
            key_index = int(self.key.rsplit('-', 1)[1])
            assert np.isnan(audio[1000 * (key_index + 1):1000 * (key_index + 1) + 500]).all()
            assert audio[0, 0] == pytest.approx(.1 * (key_index + 1))
            result = super().assemble(end, audio)
            result[:, 1] = key_index + 1
            result[:, 210] = audio[0, 0]
            result[key_index + 2, 214] = 0
            return result

    class SelectedScorer:
        audio_contract = 'sampleclock-v3'

        def __init__(self):
            self.calls = []

        def predict_scores(self, tensors, *, endpoint_seconds, half):
            assert half == 1
            assert set(endpoint_seconds) == set(tensors)
            current = {}
            for key, raw in tensors.items():
                key_index = int(key.rsplit('-', 1)[1])
                assert raw.shape == (75, 215) and raw.dtype == np.float32
                assert np.all(raw[:, 1] == key_index + 1)
                assert np.all(raw[:, 210] == np.float32(.1 * (key_index + 1)))
                assert raw[key_index + 2, 214] == 0  # Genuine gap mask is forwarded.
                end = float(raw[0, 0]) / 15
                assert endpoint_seconds[key] == pytest.approx(offsets[key] + end)
                current[key] = {'common_endpoint': end, 'source_endpoint': endpoint_seconds[key],
                                'raw': raw.copy()}
            self.calls.append(current)
            return {key: .2 + .1 * int(key.rsplit('-', 1)[1]) for key in tensors}

    harness.cursor_factory, harness.feature_factory = Cursor, Features
    harness.scorer = SelectedScorer()
    return harness, offsets


def _worker_fixture(monkeypatch, tmp_path):
    module, _, _ = load_app(monkeypatch)
    monkeypatch.setattr(module, 'Path', lambda value: tmp_path if value == '/models' else Path(value))
    reloads, loaded, predictions = [], [], []
    monkeypatch.setattr(module.models, 'reload', lambda: reloads.append(True))
    readiness = {'model_version': NEW_MODEL_VERSION, 'audio_contract': 'sampleclock-v3',
                 'selected_head': 'shots3_10', 'schema': 'matchpulse-215-v1'}

    def predict(features, *, endpoint_seconds, half):
        predictions.append((features, endpoint_seconds, half))
        return dict.fromkeys(features, .25)

    scorer = SimpleNamespace(audio_contract='sampleclock-v3', readiness=readiness, predict_scores=predict)

    def loader(target, *, model_version, device):
        assert target == (tmp_path / NEW_MODEL_VERSION).resolve()
        assert model_version == NEW_MODEL_VERSION and device == 'cpu'
        loaded.append(target)
        return scorer

    monkeypatch.setattr(classifiers, 'load_replay_scorer', loader)
    return module, scorer, reloads, loaded, predictions


def test_selected_runner_preserves_two_sources_audio_contract_features_and_original_endpoints(monkeypatch):
    harness, offsets = _selected_harness(monkeypatch)
    harness.control_override = lambda h: {
        'active': not (len(h.features) == 2 and all(len(f.assembled) == 3 for f in h.features)),
    }
    result = harness.run()
    assert result['reason'] == 'control_stopped'
    assert result['metrics']['windows'] == 6 and result['metrics']['decoder_failures'] == 0
    assert len(harness.detected) == len(set(harness.detected)) == 270
    for key, offset in offsets.items():
        calls = [call[key] for call in harness.scorer.calls if key in call]
        assert [call['common_endpoint'] for call in calls] == [5, 7, 9]
        assert [call['source_endpoint'] for call in calls] == pytest.approx([offset + n for n in (5, 7, 9)])
    for record in result['scores']:
        assert record['observation_end'] == 9 and record['observation_start'] == 4
        assert record['source_timestamp'] == pytest.approx(offsets[record['match_id']] + 9)
        assert record['model_version'] == NEW_MODEL_VERSION and record['score_kind'] == 'model'
    assert all(cursor.closed for cursor in harness.cursors)


def test_selected_seek_reopens_contract_aware_sources_without_cross_epoch_observations(monkeypatch):
    harness, offsets = _selected_harness(monkeypatch)

    def control(h):
        if h.clock.now < .5:
            return {'clock_epoch': 0}
        return {'clock_epoch': 1, 'position': 20 + h.clock.now - .5,
                'active': not (len(h.features) == 4 and all(f.assembled for f in h.features[2:]))}

    harness.control_override = control
    result = harness.run()
    assert len(harness.features) == 4 and all(not f.assembled for f in harness.features[:2])
    assert all(f.assembled == [375] for f in harness.features[2:])
    assert all(f.appended[0] == ('1:0', 300) for f in harness.features[2:])
    assert all(record['clock_epoch'] == 1 and record['observation_end'] == 25 for record in result['scores'])
    for call in harness.scorer.calls:
        for key, value in call.items():
            assert value['source_endpoint'] == pytest.approx(offsets[key] + 25)
    assert all(cursor.closed for cursor in harness.cursors)


@pytest.mark.parametrize('mode,explicit_contract', [('features_mock', False), ('real', True)])
def test_mock_and_legacy_keep_old_factory_and_scorer_signatures(monkeypatch, mode, explicit_contract):
    harness = Harness(monkeypatch)
    old_cursor, old_features = harness.cursor_factory, harness.feature_factory
    calls = []

    def cursor_factory(url, *, offset_seconds, start_index):
        return old_cursor(url, offset_seconds=offset_seconds, start_index=start_index)

    def feature_factory(key, detector, *, detector_config, source_epoch, timeline_origin_seconds):
        return old_features(key, detector, detector_config=detector_config, source_epoch=source_epoch,
                            timeline_origin_seconds=timeline_origin_seconds)

    class OldScorer:
        def predict_scores(self, tensors):
            calls.append(set(tensors))
            return dict.fromkeys(tensors, .25)

    harness.scorer = OldScorer()
    if explicit_contract:
        harness.scorer.audio_contract = 'legacy-gru'
        harness.spec.update(mode=mode, model_version=LEGACY_MODEL_VERSION, checkpoint=LEGACY_MODEL_VERSION)
    harness.cursor_factory, harness.feature_factory = cursor_factory, feature_factory
    harness.control_override = lambda h: {
        'active': not (len(h.features) == 2 and all(f.assembled for f in h.features)),
    }
    result = harness.run()
    assert calls and result['metrics']['windows'] == 2
    assert all(record['score_kind'] == ('model' if mode == 'real' else 'mock') for record in result['scores'])
    assert all(cursor.closed for cursor in harness.cursors)


def test_cpu_and_replay_worker_share_selected_loader_readiness_and_prediction_contract(monkeypatch, tmp_path):
    module, scorer, reloads, loaded, predictions = _worker_fixture(monkeypatch, tmp_path)
    cpu = module.ClassifierWorker()
    cpu.checkpoint_name = NEW_MODEL_VERSION
    cpu.load()
    assert cpu.ready() == scorer.readiness
    raw = {'cam-a': np.zeros((75, 215), np.float32)}
    endpoints = {'cam-a': 125.25}
    assert cpu.predict(raw, endpoint_seconds=endpoints, half=2) == {'cam-a': .25}
    assert predictions == [(raw, endpoints, 2)]
    gpu = module.ReplayWorker()
    gpu.classifiers = {}
    spec = {'mode': 'real', 'model_version': NEW_MODEL_VERSION, 'checkpoint': NEW_MODEL_VERSION}
    assert gpu._scorer(spec) is scorer and gpu._scorer(spec) is scorer
    assert len(loaded) == len(reloads) == 2  # Once CPU, once GPU; GPU cache is reused.
    assert gpu._scorer_phase == 'validate'


def test_replay_worker_cannot_relabel_cached_selected_checkpoint_as_another_model(monkeypatch, tmp_path):
    module, scorer, _, loaded, _ = _worker_fixture(monkeypatch, tmp_path)
    worker = module.ReplayWorker()
    worker.classifiers = {}
    spec = {'mode': 'real', 'model_version': NEW_MODEL_VERSION, 'checkpoint': NEW_MODEL_VERSION}
    assert worker._scorer(spec) is scorer
    with pytest.raises(ValueError, match='model version'):
        worker._scorer({**spec, 'model_version': LEGACY_MODEL_VERSION})
    assert len(loaded) == 1


def test_wrong_selected_readiness_version_is_never_cached(monkeypatch, tmp_path):
    module, scorer, _, loaded, _ = _worker_fixture(monkeypatch, tmp_path)
    scorer.readiness['model_version'] = LEGACY_MODEL_VERSION
    worker = module.ReplayWorker()
    worker.classifiers = {}
    with pytest.raises(ValueError, match='model version'):
        worker._scorer({'mode': 'real', 'model_version': NEW_MODEL_VERSION, 'checkpoint': NEW_MODEL_VERSION})
    assert len(loaded) == 1 and worker.classifiers == {}
