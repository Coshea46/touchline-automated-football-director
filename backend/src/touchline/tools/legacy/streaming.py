"""Historical streaming deployment/replay validation with its original $10 ledger."""

import argparse
import json
import math
import subprocess
import sys
import time
import uuid
from pathlib import Path

from touchline.ml_inference.config import APP_NAME
from touchline.ml_inference.streaming.contract import MATCHES_PER_GPU, MAX_GPU_WORKERS, MAX_WORKER_RATE
from touchline.config import cloud_environment
from touchline.paths import INFERENCE_ARTIFACTS_DIR, PROJECT_ROOT
from touchline.tools.support.streaming_budget import StreamingBudget
from touchline.tools.support.storage import atomic_json

ARTIFACTS = INFERENCE_ARTIFACTS_DIR / 'overhaul'


class Budget(StreamingBudget):
    def __init__(self):
        super().__init__(ARTIFACTS)


def save(name, value):
    atomic_json(ARTIFACTS / name, value, unique=False, trailing_newline=False)


def environment():
    return cloud_environment(credentials_path=ARTIFACTS / 'ws-credentials.json')


def deploy():
    budget = Budget()
    attempts = sum(row['name'] == 'deployment' for row in budget.data['entries'])
    if attempts >= 7:
        raise RuntimeError('Seven bounded deployment attempts used; inspect diagnostics')
    budget.reserve('deployment', .60)
    log_name = f'deployment-{attempts + 1:02d}.log'
    started = time.perf_counter()
    with (ARTIFACTS / log_name).open('w', encoding='utf-8') as output:
        result = subprocess.run([sys.executable, '-B', '-m', 'modal', 'deploy', '-m',
                                 'touchline.ml_inference.deployment.modal_app'], cwd=PROJECT_ROOT,
                                env=environment(), stdout=output, stderr=subprocess.STDOUT,
                                timeout=1200, check=False)
    save('deployment.json', {'app': APP_NAME, 'exit_code': result.returncode,
                             'wall_seconds': time.perf_counter() - started, 'log': log_name,
                             'gpu': 'T4', 'replay_container_cap': MAX_GPU_WORKERS, 'min_containers': 0})
    if result.returncode:
        raise RuntimeError('Inference deployment failed; inspect the private owned log')
    credentials = ARTIFACTS / 'ws-credentials.json'
    if credentials.exists():
        import modal

        private = json.loads(credentials.read_text(encoding='utf-8'))
        endpoint = modal.Cls.from_name(APP_NAME, 'ReplayWorker')().web.get_web_url()
        if not endpoint or not endpoint.startswith('https://'):
            raise RuntimeError('Deployed authenticated WebSocket endpoint is unavailable')
        private['url'] = endpoint.replace('https://', 'wss://', 1).rstrip('/') + '/session'
        save('ws-credentials.json', private)
    print('Deployed independent inference replay worker', flush=True)


def equivalence():
    import modal

    budget = Budget()
    if sum(row['name'] == 'overlap-equivalence' for row in budget.data['entries']) >= 3:
        raise RuntimeError('Three bounded overlap attempts used; do not retry blindly')
    row = budget.reserve('overlap-equivalence', (300 + 300 + 30) * MAX_WORKER_RATE)
    spec = {'session_id': uuid.uuid4().hex, 'mode': 'features_mock', 'model_version': 'equivalence-v1',
            'max_duration_seconds': 30, 'max_estimated_cost_usd': .25,
            'matches': [{'match_id': f'cam-{i}', 'source_url':
                f'https://match-switcher-poc-videos.s3.eu-west-2.amazonaws.com/match-{i}.mp4',
                'offset_seconds': 120} for i in (1, 2)]}
    worker = modal.Cls.from_name(APP_NAME, 'ReplayWorker').with_options(timeout=240, max_containers=1)()
    started = time.perf_counter()
    handle = worker.validate_sources.spawn(spec)
    row['call_id'] = handle.object_id
    budget.flush()
    try:
        result = handle.get(timeout=300)
    except Exception as exc:  # noqa: BLE001 - retain reservation and sanitize provider errors
        row.update(state='failed-or-unconfirmed', error_type=type(exc).__name__)
        budget.flush()
        handle.cancel(terminate_containers=True)
        raise RuntimeError(f'Overlap proof failed ({type(exc).__name__}); inspect inference logs') from None
    budget.settle(row, time.perf_counter() - started)
    save('equivalence.json', result)
    print({'equivalence_passed': result['passed'], 'windows': len(result['windows']),
           'max_absolute_difference': max(r['max_absolute_difference'] for r in result['windows'])}, flush=True)
    if not result['passed']:
        raise RuntimeError('Feature equivalence failed; broader benchmarks remain gated')
    # Validation uses a different timeout pool. Wait for its idle scale-down
    # before a monitored session; never accidentally overlap the GPU pools.
    for _ in range(4):
        time.sleep(10)
        stats = worker.validate_sources.get_current_stats()
        record = {'runners': stats.num_total_runners, 'inputs': stats.num_running_inputs,
                  'backlog': stats.backlog}
        save('equivalence-idle.json', record)
        if not any(record.values()):
            return
    raise RuntimeError('Validation GPU pool did not scale down; stop before new sessions')


def probe(matches, seconds, mode='features_mock'):
    proof = json.loads((ARTIFACTS / 'equivalence.json').read_text())
    if not proof['passed']:
        raise RuntimeError('Real overlap equivalence must pass before replay benchmarks')
    budget = Budget()
    if sum(row['name'].startswith('paced-probe-') for row in budget.data['entries']) >= 11:
        raise RuntimeError('Eleven bounded replay probes used; inspect results')
    workers = math.ceil(matches / MATCHES_PER_GPU)
    reserve = (seconds + 180 + 330) * MAX_WORKER_RATE * 3 * workers
    row = budget.reserve(f'paced-probe-{matches}', reserve)
    env = environment()
    env.update({'MATCHPULSE_MONITOR': 'true', 'MATCHPULSE_ML_MODE': mode,
                'MATCHPULSE_MODEL_VERSION': 'gru-stage1-02de8309' if mode == 'real' else 'streaming-mock-v1',
                'MATCHPULSE_GPU_WORKERS': str(workers),
                'MATCHPULSE_OFFSETS_JSON': json.dumps({f'cam-{i}': 120 for i in range(1, matches + 1)})})
    env.pop('MATCHPULSE_MOCK_SCORES_JSON', None)
    env.pop('MATCHPULSE_ML_CHECKPOINT', None)
    if mode == 'real':
        readiness = json.loads((ARTIFACTS / 'checkpoint-readiness.json').read_text())
        if not readiness.get('readiness_verified'):
            raise RuntimeError('Real inference requires verified checkpoint readiness')
        env['MATCHPULSE_ML_CHECKPOINT'] = readiness['checkpoint']
    else:
        env['MATCHPULSE_MOCK_SCORES_JSON'] = json.dumps({f'cam-{i}': .1 if i != 2 else .9 for i in range(1, matches + 1)})
    started = time.perf_counter()
    result = subprocess.run([sys.executable, '-B', '-m', 'touchline.tools.validation.replay_probe', '--allow-cloud',
                             '--matches', str(matches), '--seconds', str(seconds),
                             '--startup-timeout', '120', '--budget', str(reserve)],
                             env=env, cwd=PROJECT_ROOT, capture_output=True, text=True,
                             timeout=seconds + 240, check=False)
    suffix = sum(row['name'].startswith('paced-probe-') for row in budget.data['entries'])
    (ARTIFACTS / f'probe-{matches}-{suffix}.log').write_text(result.stderr, encoding='utf-8')
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        save(f'probe-{matches}-{suffix}-failure.json', {'exit_code': result.returncode,
                                                       'error': 'Probe did not return structured results'})
        raise RuntimeError('Inspect sanitized probe stderr') from None
    save(f'probe-{matches}-{suffix}.json', report)
    if report.get('owned_worker_cancelled'):
        budget.settle(row, time.perf_counter() - started, workers)
    else:
        row['state'] = 'cleanup-unconfirmed'
        budget.flush()
    print({'matches': matches, 'passed': report.get('passed'), 'latency': report.get('latency'),
           'errors': report.get('errors'), 'cleanup': report.get('owned_worker_cancelled')}, flush=True)
    if result.returncode:
        raise RuntimeError('Paced replay probe failed; do not queue blind replacements')


def idle():
    import modal

    worker = modal.Cls.from_name(APP_NAME, 'ReplayWorker')()
    pools = {}
    for name, function in (('replay_run', worker.run), ('replay_web', worker.web),
            ('legacy_features', modal.Cls.from_name(APP_NAME, 'FeatureWorker')().extract),
            ('installed_classifier', modal.Cls.from_name(APP_NAME, 'ClassifierWorker')(
                checkpoint_name='gru-stage1-02de8309/classifier.pt').ready)):
        stats = function.get_current_stats()
        pools[name] = {'runners': stats.num_total_runners, 'inputs': stats.num_running_inputs,
                       'backlog': stats.backlog}
    record = {'app': APP_NAME, **{field: max(pool[field] for pool in pools.values())
              for field in ('runners', 'inputs', 'backlog')}, 'pools': pools}
    save('replay-idle.json', record)
    print(record, flush=True)


def install(trusted_checkpoint):
    from touchline.tools.cloud.checkpoints import install_checkpoint

    budget = Budget()
    if any(row['name'] == 'checkpoint-readiness' for row in budget.data['entries']):
        raise RuntimeError('Checkpoint readiness already attempted; inspect the result before any retry')
    row = budget.reserve('checkpoint-readiness', .04)
    before = time.perf_counter()
    result = install_checkpoint(
        trusted_checkpoint,
        version='gru-stage1-02de8309',
        expected_sha256='02de83095e5eaaf2f22978e89eec8515633ecd680ba0e9834cfb46d8cbea9319',
        allow_cloud=True, timeout_seconds=120)
    save('checkpoint-readiness.json', result)
    budget.settle(row, time.perf_counter() - before)
    print({'readiness_verified': result['readiness_verified'], 'checkpoint': result['checkpoint'],
           'sha256': result['sha256']}, flush=True)


def diagnose():
    from touchline.ml_inference.streaming.client import ReplayClient

    budget = Budget()
    if any(row['name'] == 'checkpoint-gpu-diagnostic' for row in budget.data['entries']):
        raise RuntimeError('The bounded checkpoint diagnostic was already attempted')
    row = budget.reserve('checkpoint-gpu-diagnostic', .12)
    client, session_id = ReplayClient(), uuid.uuid4().hex
    client.update_control(session_id, {'active': True, 'playing': False, 'position': 0,
        'clock_epoch': 0, 'expires_at': time.time() + 120})
    spec = {'session_id': session_id, 'mode': 'real', 'model_version': 'gru-stage1-02de8309',
        'checkpoint': 'gru-stage1-02de8309/classifier.pt', 'max_duration_seconds': 10,
        'max_estimated_cost_usd': .2, 'matches': [{'match_id': 'cam-1', 'source_url':
            'https://match-switcher-poc-videos.s3.eu-west-2.amazonaws.com/match-1.mp4', 'offset_seconds': 120}]}
    started, owned = time.perf_counter(), None
    try:
        owned = client.start(spec)
        result = owned.handles[0].get(timeout=90)
        save('checkpoint-gpu-diagnostic.json', result)
        print({key: result.get(key) for key in ('state', 'reason', 'error_type')}, flush=True)
    finally:
        client.stop(session_id, owned)
        budget.settle(row, time.perf_counter() - started)


def setup_ws():
    import modal

    credentials = ARTIFACTS / 'ws-credentials.json'
    if not credentials.exists():
        token = modal.Workspace.from_context().proxy_tokens.create(name='matchpulse-inference-replay')
        save('ws-credentials.json', {'token': token.token_id + '.' + token.token_secret,
                                    'proxy_token_id': token.token_id, 'url': None})
    private = json.loads(credentials.read_text(encoding='utf-8'))
    modal.Secret.objects.create('matchpulse-inference-replay-auth',
        {'MATCHPULSE_REPLAY_WS_TOKEN': private['token']}, allow_existing=True)
    print('Configured inference-only authenticated WebSocket credentials; saved privately', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['deploy', 'equivalence', 'probe', 'idle', 'install', 'diagnose', 'setup-ws'])
    parser.add_argument('--allow-cloud', action='store_true')
    parser.add_argument('--matches', type=int, choices=range(1, 8), default=1)
    parser.add_argument('--seconds', type=int, choices=range(10, 61), default=20)
    parser.add_argument('--mode', choices=['features_mock', 'real'], default='features_mock')
    parser.add_argument('--trusted-checkpoint', type=Path,
                        help='Explicit local portable checkpoint for the install action')
    args = parser.parse_args()
    if not args.allow_cloud:
        parser.error('Explicit --allow-cloud authorization required')
    if args.action == 'install' and args.trusted_checkpoint is None:
        parser.error('Install requires an explicit --trusted-checkpoint')
    if args.action == 'deploy':
        deploy()
    elif args.action == 'equivalence':
        equivalence()
    elif args.action == 'probe':
        probe(args.matches, args.seconds, args.mode)
    elif args.action == 'install':
        install(args.trusted_checkpoint)
    elif args.action == 'diagnose':
        diagnose()
    elif args.action == 'setup-ws':
        setup_ws()
    else:
        idle()


if __name__ == '__main__':
    main()
