"""Small, validated replay contracts shared by CPU orchestration and GPU workers."""

import math
import re
from urllib.parse import urlsplit

from touchline.ml_inference.config import APP_NAME

REPLAY_DICT = 'matchpulse-inference-replay'
MAX_MATCHES = 7
MAX_GPU_WORKERS = 4
MATCHES_PER_GPU = 2
MAX_SESSION_SECONDS = 3600
MAX_SESSION_BUDGET_USD = 20
# Existing deployed workers accept this per-input ceiling. Larger service
# allowances are partitioned without changing the deployed inference runtime.
MAX_WORKER_BUDGET_USD = 10
MAX_WORKER_RATE = .000164 + 8 * .0000131 + 8 * .00000222
_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,120}$')


def identifier(value, name):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f'{name} must be a bounded alphanumeric identifier')
    return value


def number(value, name, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be finite')
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f'{name} outside supported range')
    return float(value)


def validate_session(spec, *, allow_local=False):
    if APP_NAME != 'matchpulse-inference' or not isinstance(spec, dict):
        raise ValueError('Invalid inference application/session')
    identifier(spec.get('session_id'), 'session_id')
    if spec.get('mode') not in ('real', 'features_mock'):
        raise ValueError('Explicit real/features_mock monitoring mode required')
    version = spec.get('model_version')
    if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}', version):
        raise ValueError('model_version must be a bounded version identifier')
    duration = number(spec.get('max_duration_seconds', 300), 'max_duration_seconds', 5, MAX_SESSION_SECONDS)
    ceiling = number(spec.get('max_estimated_cost_usd', 1), 'max_estimated_cost_usd', .01, MAX_SESSION_BUDGET_USD)
    number(spec.get('start_position', 0), 'start_position')
    matches = spec.get('matches')
    if not isinstance(matches, list) or not 1 <= len(matches) <= MAX_MATCHES:
        raise ValueError('Register one to seven matches')
    workers = spec.get('gpu_workers', math.ceil(len(matches) / MATCHES_PER_GPU))
    if type(workers) is not int or not 1 <= workers <= min(MAX_GPU_WORKERS, len(matches)):
        raise ValueError('gpu_workers must select one to four bounded physical workers')
    estimate = (duration + 300 + 30) * MAX_WORKER_RATE * workers
    if estimate > ceiling:
        raise ValueError('Requested session exceeds its estimated compute allowance')
    if 'shard_id' in spec and spec['shard_id'] not in tuple(str(i) for i in range(MAX_GPU_WORKERS)):
        raise ValueError('shard_id must identify a bounded physical worker')
    ids = []
    for match in matches:
        if not isinstance(match, dict):
            raise TypeError('Invalid match registration')
        ids.append(identifier(match.get('match_id'), 'match_id'))
        number(match.get('offset_seconds', 0), 'offset_seconds')
        url = match.get('source_url')
        if not isinstance(url, str) or len(url) > 16384:
            raise ValueError('Media source must be a bounded authorized URL')
        parsed = urlsplit(url)
        if ((parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password)
                and (not allow_local or parsed.scheme not in ('', 'file'))):
            raise ValueError('Production media sources require HTTPS without embedded credentials')
    if len(set(ids)) != len(ids):
        raise ValueError('Match IDs must be unique')
    if spec['mode'] == 'real':
        checkpoint = spec.get('checkpoint')
        if not isinstance(checkpoint, str) or not checkpoint or '..' in checkpoint or checkpoint.startswith(('/', '\\')):
            raise ValueError('Real mode requires a versioned portable checkpoint')
    return spec


def validate_control(session_id, control):
    identifier(session_id, 'session_id')
    if not isinstance(control, dict) or control.get('session_id', session_id) != session_id:
        raise ValueError('Control belongs to a different session')
    number(control.get('position'), 'position')
    number(control.get('expires_at'), 'expires_at')
    if type(control.get('active')) is not bool or type(control.get('playing')) is not bool:
        raise ValueError('Control active/playing flags must be boolean')
    if 'startup_waiting' in control and type(control['startup_waiting']) is not bool:
        raise ValueError('Control startup_waiting flag must be boolean')
    if type(control.get('clock_epoch', 0)) is not int or control.get('clock_epoch', 0) < 0:
        raise ValueError('clock_epoch must be nonnegative')
    return control
