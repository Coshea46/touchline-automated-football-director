"""Historical finite HTML-player validation server with explicit paid-inference opt-in."""

import argparse
import asyncio
import json
import logging
import os
import time

from touchline.main import AppController

from touchline.config.cloud import cloud_environment as environment
from touchline.tools.support.streaming_budget import StreamingBudget as Budget, save
from touchline.ml_inference.streaming.contract import MAX_WORKER_RATE


async def serve(seconds, mode='features_mock'):
    os.environ.update(environment())
    os.environ.update({'MATCHPULSE_MONITOR': 'true', 'MATCHPULSE_ML_MODE': 'features_mock',
        'MATCHPULSE_MODEL_VERSION': 'browser-mock-v1', 'MATCHPULSE_MATCH_COUNT': '2',
        'MATCHPULSE_GPU_WORKERS': '1',
        'MATCHPULSE_MOCK_SCORES_JSON': json.dumps({'cam-1': .1, 'cam-2': .9}),
        'MATCHPULSE_OFFSETS_JSON': json.dumps({'cam-1': 120, 'cam-2': 120}),
        'MATCHPULSE_SESSION_SECONDS': str(seconds), 'MATCHPULSE_SESSION_BUDGET_USD': '.5',
        'MATCHPULSE_ALLOWED_ORIGINS': 'http://localhost:3020,http://127.0.0.1:3020'})
    if mode == 'real':
        os.environ.pop('MATCHPULSE_MOCK_SCORES_JSON', None)
        os.environ.update({'MATCHPULSE_ML_MODE': 'real',
            'MATCHPULSE_MODEL_VERSION': 'gru-stage1-02de8309',
            'MATCHPULSE_ML_CHECKPOINT': 'gru-stage1-02de8309/classifier.pt'})
    budget = Budget()
    row = budget.reserve('html-browser-validation', (seconds + 330) * MAX_WORKER_RATE * 3)
    controller = AppController(host='127.0.0.1', port=8020)
    session = controller.session
    before = time.perf_counter()
    task = asyncio.create_task(controller.start())
    print('Browser validation backend on 127.0.0.1:8020; bounded lifetime', seconds, flush=True)
    observations = []
    try:
        while time.perf_counter() - before < seconds:
            await asyncio.sleep(2)
            observations.append({'elapsed': time.perf_counter() - before, 'state': session.state,
                'position': session.position, 'playing': session.playing,
                'epoch': session.clock_epoch, 'current_match_id': session.director.current_match_id,
                'clients': len(session.clients), 'metrics': dict(session.metrics)})
            save('browser-progress.json', {'observations': observations})
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await session.close()
        report = {'mode': mode, 'observations': observations,
                  'metrics': session.metrics, 'worker_health': session.worker_health,
                  'elapsed_seconds': time.perf_counter() - before}
        save('browser.json', report)
        if session.metrics.get('worker_stop_succeeded'):
            budget.settle(row, time.perf_counter() - before)
        print(json.dumps(report['metrics']), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-cloud', action='store_true')
    parser.add_argument('--seconds', type=int, choices=range(30, 181), default=100)
    parser.add_argument('--mode', choices=['features_mock', 'real'], default='features_mock')
    args = parser.parse_args()
    if not args.allow_cloud:
        parser.error('--allow-cloud is required')
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(serve(args.seconds, args.mode))


if __name__ == '__main__':
    main()
