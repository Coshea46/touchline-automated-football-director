"""Persistent local website backend with separately bounded optional inference."""

import argparse
import asyncio
import json
import logging
import math
import os
from dataclasses import replace

from touchline.config.local import load_local_environment
from touchline.main import AppController
from touchline.paths import INFERENCE_ARTIFACTS_DIR
from touchline.ml_inference.models.release import LEGACY_MODEL_VERSION, NEW_MODEL_VERSION
from touchline.ml_inference.streaming.contract import MATCHES_PER_GPU, MAX_SESSION_BUDGET_USD, MAX_WORKER_RATE
from touchline.replay import SessionConfig, SourceRegistry

CREDENTIALS_PATH = INFERENCE_ARTIFACTS_DIR / 'overhaul/ws-credentials.json'


def configuration(args, env=None):
    """Reuse production validation; a launch never renews its finite allowance."""
    if type(args.seconds) is not int or not 30 <= args.seconds <= 3600:
        raise ValueError('Choose 30 to 3600 inference seconds')
    if (isinstance(args.budget, bool) or not isinstance(args.budget, (int, float))
            or not math.isfinite(args.budget) or not .01 <= args.budget <= MAX_SESSION_BUDGET_USD):
        raise ValueError(f'Choose a finite estimated inference budget between $0.01 and ${MAX_SESSION_BUDGET_USD}')
    if args.model not in ('selected', 'legacy'):
        raise ValueError('Choose the selected model or explicit legacy rollback')
    effective = dict(os.environ if env is None else env)
    effective.update(MATCHPULSE_MONITOR='true' if args.allow_cloud else 'false',
                     MATCHPULSE_SESSION_SECONDS=str(args.seconds),
                     MATCHPULSE_SESSION_BUDGET_USD=str(args.budget),
                     MATCHPULSE_REPLAY_TRANSPORT='websocket')
    checkpoint = NEW_MODEL_VERSION if args.model == 'selected' else LEGACY_MODEL_VERSION
    effective.update(MATCHPULSE_ML_MODE='real' if args.allow_cloud else 'disabled',
                     MATCHPULSE_ML_CHECKPOINT=checkpoint if args.allow_cloud else '',
                     MATCHPULSE_MODEL_VERSION=checkpoint if args.allow_cloud else 'disabled')
    if args.matches is not None:
        if type(args.matches) is not int or not 1 <= args.matches <= 7:
            raise ValueError('Choose one to seven matches')
        effective['MATCHPULSE_MATCH_COUNT'] = str(args.matches)
    config = replace(SessionConfig.from_env(effective), max_restarts=0)
    registry = SourceRegistry.from_env(effective)
    workers = config.gpu_workers or math.ceil(len(registry.sources) / MATCHES_PER_GPU)
    allowance = (config.max_duration_seconds + 330) * workers * MAX_WORKER_RATE
    if args.allow_cloud and allowance > config.max_estimated_cost_usd:
        raise ValueError('Inference duration and worker count exceed the per-launch budget')
    return registry, config


def private_ws_credentials(env=None, *, path=CREDENTIALS_PATH):
    """Read only existing app credentials; no SDK, weights or deployment access."""
    env = os.environ if env is None else env
    url, token = env.get('MATCHPULSE_MODAL_WS_URL'), env.get('MATCHPULSE_MODAL_WS_TOKEN')
    if bool(url) != bool(token):
        raise ValueError('Configure both MATCHPULSE_MODAL_WS_URL and MATCHPULSE_MODAL_WS_TOKEN')
    if not url:
        try:
            if path.stat().st_size > 16384:
                raise ValueError
            value = json.loads(path.read_text(encoding='utf-8'))
            url, token = value['url'], value['token']
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError('Existing private inference WebSocket credentials are unavailable') from None
    # Construction validates TLS endpoint/token without opening a socket or
    # importing/authenticating the Modal SDK. Never log either supplied value.
    from touchline.ml_inference.streaming.ws_client import ReplayWebSocketClient

    ReplayWebSocketClient(url, token)
    return {'MATCHPULSE_MODAL_WS_URL': url, 'MATCHPULSE_MODAL_WS_TOKEN': token}


async def serve(args):
    load_local_environment()
    registry, config = configuration(args)
    prepared, preparation_error = None, None
    if not getattr(args, 'offline', False):
        try:
            credentials = private_ws_credentials()
        except ValueError:
            if args.allow_cloud:
                raise
            preparation_error = 'credentials_unavailable'
        else:
            os.environ.update(credentials)
            checkpoint = NEW_MODEL_VERSION if args.model == 'selected' else LEGACY_MODEL_VERSION
            prepared = replace(config, monitor=True, mode='real', model_version=checkpoint, checkpoint=checkpoint)
    if args.no_captions:
        os.environ['MATCHPULSE_CAPTIONS_ENABLED'] = 'false'
    origins = f'http://localhost:{args.frontend_port},http://127.0.0.1:{args.frontend_port}'
    os.environ['MATCHPULSE_ALLOWED_ORIGINS'] = origins
    os.environ['MATCHPULSE_API_ALLOWED_ORIGINS'] = origins
    controller = AppController(registry=registry, config=config, host='127.0.0.1', port=args.port,
                               api_host='127.0.0.1', api_port=args.api_port, api_enabled=True,
                               prepared_config=prepared, preparation_error=preparation_error)
    print(f'Touchline local: WebSocket 127.0.0.1:{args.port}; API 127.0.0.1:{args.api_port}. '
          'Keep this terminal running; Ctrl+C stops the backend.', flush=True)
    if args.allow_cloud:
        print(f'Inference is bounded to {args.seconds}s and ${args.budget:.2f} estimated budget; '
              'expiry leaves the website connected and does not renew paid work.', flush=True)
    elif prepared is not None:
        print('Auto On can start prepared real monitoring; no GPU starts before that request. '
              'Configured live captions have their own finite allowance.', flush=True)
    else:
        print('Real monitoring is unavailable or explicitly offline; the website remains usable.', flush=True)
    await controller.start()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--allow-cloud', action='store_true', help='Start one bounded real-inference session on playback')
    modes.add_argument('--offline', action='store_true', help='Disable the ability to start cloud inference')
    parser.add_argument('--seconds', type=int, default=2400, help='Service inference allowance, 30-3600 seconds')
    parser.add_argument('--budget', type=float, default=10, help=f'Service estimated inference USD ceiling, $0.01-${MAX_SESSION_BUDGET_USD}')
    parser.add_argument('--matches', type=int, default=None, help='Default: configured sources, up to seven')
    parser.add_argument('--model', choices=('selected', 'legacy'), default='selected')
    parser.add_argument('--no-captions', action='store_true', help='Disable live caption provider requests')
    parser.add_argument('--port', type=int, default=8030)
    parser.add_argument('--api-port', type=int, default=8031)
    parser.add_argument('--frontend-port', type=int, default=3030)
    args = parser.parse_args()
    ports = (args.port, args.api_port, args.frontend_port)
    if not all(1024 <= port <= 65535 for port in ports) or len(set(ports)) != 3:
        parser.error('Choose distinct backend/API/frontend ports between 1024 and 65535')
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(serve(args))
    except ValueError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
