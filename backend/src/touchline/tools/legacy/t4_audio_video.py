"""Private, bounded T4 audio/video validation; never a research controller."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from touchline.config.cloud import window_environment as environment
from touchline.tools.support.window_budget import ARTIFACTS, BudgetedMethod, WindowValidationBudget as TaskBudget, save


def digest(array):
    return hashlib.sha256(memoryview(array).cast('B')).hexdigest()


def prepare(research_root):
    import av
    import numpy as np
    from matchpulse.features.video import from_file

    from touchline.ml_inference.features.sample_preparation import decode_broadcast_audio
    from touchline.ml_inference.window_api.transport import encode_request, payload_bytes
    from touchline.ml_inference.features.validation import validate_audio, validate_window

    source_root = Path(research_root).expanduser().resolve(strict=True) / 'artifacts/kaggle-upload/videos/england_epl/2015-2016'
    # Existing, authorized local source files. Decode only the requested interval;
    # no full source copy, media output, source URL or dataset download.
    source_names = ['2015-08-16 - 18-00 Manchester City 3 - 0 Chelsea',
                    '2015-08-29 - 17-00 Chelsea 1 - 2 Crystal Palace']
    windows, waveforms, packets, metadata = {}, {}, {}, {}
    for index, name in enumerate(source_names, 1):
        key = f'match_{index:02d}'
        path = source_root / name / '1_720p.mkv'
        started = time.perf_counter()
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            video_origin = float((stream.start_time or 0) * stream.time_base)
        window = from_file(path, start=120, duration_seconds=5)
        frames = np.stack(window.frames)
        audio = decode_broadcast_audio(path, 120, video_origin)
        validate_window(frames)
        validate_audio(audio)
        rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        if rms < 1e-5:
            raise ValueError('Selected real football interval must have non-silent audio')
        decode_seconds = time.perf_counter() - started
        packet = encode_request(key, frames, audio)
        windows[key], waveforms[key], packets[key] = frames, audio, packet
        metadata[key] = {
            'source': name + '/1_720p.mkv', 'source_size_bytes': path.stat().st_size,
            'window_start_seconds': 120, 'window_duration_seconds': 5,
            'video_origin_seconds': video_origin,
            'video_shape': list(frames.shape), 'video_dtype': str(frames.dtype),
            'audio_shape': list(audio.shape), 'audio_dtype': str(audio.dtype),
            'sample_rate': 16000, 'audio_rms': rms,
            'input_sha256': digest(frames), 'audio_sha256': digest(audio),
            'source_timestamps_relative_to_window': window.source_timestamps.tolist(),
            'target_timestamps_relative_to_window': window.timestamps.tolist(),
            'local_decode_resample_seconds': decode_seconds,
            'video_raw_bytes': frames.nbytes, 'audio_raw_bytes': audio.nbytes,
            'video_compressed_bytes': payload_bytes(packet['video']),
            'audio_compressed_bytes': len(packet['audio']['block']),
            'encoding_seconds': packet['encoding_seconds'],
        }
        print('Prepared synchronized broadcast window', key, list(frames.shape),
              list(audio.shape), 'audio RMS', round(rms, 5), flush=True)
    save('sources.json', metadata)
    return windows, waveforms, packets, metadata


def provider_snapshot(worker, name):
    since = datetime.fromtimestamp(json.loads((ARTIFACTS / 'budget.json').read_text())['entries'][0]['time']).astimezone()
    records = {}
    for method_name in ['extract', 'diagnose_pair']:
        method = getattr(worker, method_name)
        current = method.get_current_stats()
        stats = asdict(method.stats(since=since, all_variants=True))
        stats['since'], stats['until'] = str(stats['since']), str(stats['until'])
        records[method_name] = {
            'current': {'runners': current.num_total_runners,
                        'running_inputs': current.num_running_inputs, 'backlog': current.backlog},
            'usage': stats,
        }
    save(name, records)
    return records


def cleanup(worker, failed):
    idle = None
    for _ in range(3):
        time.sleep(10)
        stats = worker.extract.get_current_stats()
        idle = {'runners': stats.num_total_runners, 'running_inputs': stats.num_running_inputs,
                'backlog': stats.backlog, 'time': time.time()}
        save('idle-stats.json', idle)
        if not any(idle[key] for key in ['runners', 'running_inputs', 'backlog']):
            break
    provider_snapshot(worker, 'provider-final-stats.json')
    stopped = failed or any(idle[key] for key in ['runners', 'running_inputs', 'backlog'])
    if stopped:
        subprocess.run([sys.executable, '-B', '-m', 'modal', 'app', 'stop',
                        'matchpulse-inference', '--yes'], env=environment(), check=True, timeout=60)
    save('cleanup.json', {'app': 'matchpulse-inference', 'stopped': stopped,
                          'scaled_to_zero': not any(idle[key] for key in ['runners', 'running_inputs', 'backlog']),
                          'observed': idle, 'time': time.time()})
    print('Inference cleanup', {'stopped': stopped, 'last_observed': idle}, flush=True)


def validate(research_root):
    from unittest.mock import patch

    import numpy as np

    from touchline.ml_inference.window_api import api
    from touchline.ml_inference.models.classifiers import MockInterestScorer
    from touchline.ml_inference.config import InferenceConfig
    from touchline.ml_inference.window_api.modal_client import ModalFeatureExtractor, _get_result
    from touchline.ml_inference.window_api.service import InferenceService
    from touchline.ml_inference.features.validation import validate_features

    deployment = json.loads((ARTIFACTS / 'deployment.json').read_text())
    if deployment['exit_code'] != 0 or deployment['gpu'] != 'T4':
        raise RuntimeError('Successful T4 deployment required before validation')
    os.environ.update(environment())
    config = InferenceConfig.from_env()
    assert config.gpu == 'T4' and config.worker_concurrency == config.max_inflight == 1
    budget = TaskBudget()
    if sum(row['name'] == 't4-equivalence-pair' for row in budget.data['entries']) >= 2:
        raise RuntimeError('Bounded T4 equivalence attempts exhausted; no blind replacements')
    windows, audio, packets, metadata = prepare(research_root)
    client = ModalFeatureExtractor(config).prepare()
    worker = client._get_worker()
    provider_snapshot(worker, 'provider-initial-stats.json')
    failed = True
    try:
        method = BudgetedMethod(worker.diagnose_pair, budget, 't4-equivalence-pair')
        started = time.perf_counter()
        results = _get_result(method.spawn(packets['match_01'], packets['match_02']), client.sdk, 300)
        elapsed = time.perf_counter() - started
        summary = []
        tensors = []
        for index, result in enumerate(results):
            key = ['match_01', 'match_02', 'match_01'][index]
            features = result.pop('features')
            validate_features(features, missing_audio=False)
            assert result['match_id'] == key
            assert result['input_sha256'] == metadata[key]['input_sha256']
            assert result['audio_sha256'] == metadata[key]['audio_sha256']
            assert result['input_unchanged'] and result['audio_unchanged']
            assert 'T4' in result['runtime']['gpu']
            assert result['runtime']['detector_device'].startswith('cuda')
            assert result['audio_available_intervals'] == 75 and result['audio_feature_nonzero']
            assert result['frames_inferred'] == 75
            result.update(shape=list(features.shape), dtype=str(features.dtype),
                          feature_bytes=features.nbytes, finite=bool(np.isfinite(features).all()))
            summary.append(result)
            tensors.append(features)
        save('diagnostics.json', {'round_trip_seconds': elapsed, 'results': summary,
                                 'nonexecution_residual_seconds': elapsed - sum(r['remote_wall_seconds'] for r in summary),
                                 'residual_note': 'Combined submission/upload/scheduling/cold initialization/return; not pure network time'})
        if len(results) != 3 or not all(r['gate']['passed'] for r in results[:2]):
            raise RuntimeError('Full-audio equivalence failed; broader benchmarks forbidden')
        assert results[2]['repeated_A_after_B_equal'] and np.array_equal(tensors[0], tensors[2])
        assert len({r['runtime']['container_id'] for r in results}) == 1
        print('Full-audio T4 equivalence passed', [r['gate']['max_absolute_difference'] for r in results[:2]], flush=True)

        class MeasuredWorker:
            extract = BudgetedMethod(worker.extract, budget, 'public-full-audio-extraction')

        cases = [('one-match', ['match_01'], {}, 'match_01'),
                 ('two-windows', ['match_01', 'match_02'], {}, 'match_01'),
                 ('challenger-wins', ['match_01', 'match_02'], {'match_01': .4, 'match_02': .8}, 'match_02'),
                 ('staying-bonus', ['match_01', 'match_02'], {'match_01': .5, 'match_02': .55}, 'match_01')]
        public = []
        for name, ids, scores, expected in cases:
            extractor = ModalFeatureExtractor(config, sdk=client.sdk)
            extractor._worker = MeasuredWorker()

            class RecordingScorer(MockInterestScorer):
                def predict_scores(self, features):
                    self.seen = sorted(features)
                    t = time.perf_counter()
                    result = super().predict_scores(features)
                    self.seconds = time.perf_counter() - t
                    return result

            scorer = RecordingScorer(scores)
            service = InferenceService(config, extractor=extractor, scorer=scorer)
            started = time.perf_counter()
            with patch.object(api, '_service', return_value=service):
                selected = api.select_best_match('match_01', {key: windows[key] for key in ids},
                                                  {key: audio[key] for key in ids})
            elapsed = time.perf_counter() - started
            assert selected == expected and getattr(scorer, 'seen', None) == sorted(ids), 'A failed-extraction fallback is not a successful GPU result'
            assert all(digest(windows[key]) == metadata[key]['input_sha256'] and digest(audio[key]) == metadata[key]['audio_sha256'] for key in ids)
            public.append({'case': name, 'matches': len(ids), 'selected': selected, 'expected': expected,
                           'seconds': elapsed, 'mock_classifier_seconds': scorer.seconds,
                           'metrics': extractor.last_metrics})
            save('public-method.json', public)
            print('Public full-audio GPU path', name, 'selected', selected, 'seconds', round(elapsed, 3), flush=True)
        failed = False
        save('result.json', {'passed': True, 'equivalence_passed': True,
                             'public_cases': len(public), 'distinct_matches': 2,
                             'football_windows': 2, 'gru_tested': False})
    finally:
        client.cancel_pending()
        cleanup(worker, failed)


def main():
    # Local preparation only. Paid work is routed through cloud_validation's
    # explicit --allow-cloud switch and persistent budget ledger.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--research-root', type=Path, required=True,
                        help='Explicit research checkout containing authorized validation recordings')
    args = parser.parse_args()
    prepare(args.research_root)


if __name__ == '__main__':
    main()
