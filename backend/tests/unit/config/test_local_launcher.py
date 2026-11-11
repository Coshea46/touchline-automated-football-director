"""Offline tests for unit config test_local_launcher."""


import asyncio
import json
from types import SimpleNamespace
import pytest
from touchline import local
from touchline.ml_inference.models.release import LEGACY_MODEL_VERSION, NEW_MODEL_VERSION


def args(**values):
    defaults = {'allow_cloud': False, 'seconds': 2400, 'budget': 10, 'model': 'selected', 'matches': None,
                'offline': False, 'no_captions': False, 'port': 8030, 'api_port': 8031, 'frontend_port': 3030}
    return SimpleNamespace(**{**defaults, **values})


def test_cli_default_budget_remains_ten_dollars_and_duration_forty_minutes(monkeypatch):
    parsed = []

    async def serve(options):
        parsed.append(options)

    monkeypatch.setattr(local, 'serve', serve)
    monkeypatch.setattr('sys.argv', ['touchline.local'])
    local.main()
    assert len(parsed) == 1
    assert parsed[0].budget == 10 and parsed[0].seconds == 2400
    assert not parsed[0].allow_cloud


def test_persistent_cpu_default_cannot_inherit_paid_monitoring_or_mock_mode():
    registry, config = local.configuration(args(), {'MATCHPULSE_MONITOR': 'true',
        'MATCHPULSE_ML_MODE': 'features_mock', 'MATCHPULSE_MODEL_VERSION': 'old-model'})
    assert len(registry.sources) == 7
    assert not config.monitor and config.mode == config.model_version == 'disabled'
    assert config.max_restarts == 0


@pytest.mark.parametrize('model,version', [('selected', NEW_MODEL_VERSION), ('legacy', LEGACY_MODEL_VERSION)])
@pytest.mark.parametrize('budget', [10, 20])
def test_explicit_real_launch_uses_a_finite_model_allowance_and_preserves_source_bindings(model, version, budget):
    options = args()
    options.allow_cloud, options.model, options.budget = True, model, budget
    registry, config = local.configuration(options, {'MATCHPULSE_MATCH_COUNT': '2',
        'MATCHPULSE_OFFSETS_JSON': '{"cam-1":120,"cam-2":260.125}', 'MATCHPULSE_DIRECTOR_MODE': 'relaxed'})
    assert config.monitor and config.mode == 'real'
    assert config.model_version == config.checkpoint == version
    assert config.max_duration_seconds == 2400 and config.max_estimated_cost_usd == budget
    assert config.max_restarts == 0 and config.director_mode == 'relaxed'
    assert [source.offset_seconds for source in registry.sources] == [120, 260.125]


@pytest.mark.parametrize('field,value', [('seconds', 29), ('seconds', 3601), ('seconds', True),
    ('budget', .001), ('budget', 20.01), ('budget', float('nan')), ('budget', True), ('matches', 8)])
def test_invalid_local_bounds_fail_before_server_or_cloud_creation(field, value):
    options = args()
    setattr(options, field, value)
    with pytest.raises(ValueError):
        local.configuration(options, {})


def test_cloud_reservation_must_fit_all_configured_workers():
    options = args()
    options.allow_cloud, options.budget = True, .01
    with pytest.raises(ValueError, match='per-launch budget'):
        local.configuration(options, {})


def test_private_credentials_are_existing_app_only_and_never_connect_or_read_weights(monkeypatch, tmp_path):
    monkeypatch.setattr('touchline.ml_inference.streaming.ws_client.websockets.connect',
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError('No socket authorized')))
    credential_file = tmp_path / 'credentials.json'
    credential_file.write_text(json.dumps({'url': 'wss://fixture.modal.run/session', 'token': 'fixture-token'}))
    values = local.private_ws_credentials({}, path=credential_file)
    assert set(values) == {'MATCHPULSE_MODAL_WS_URL', 'MATCHPULSE_MODAL_WS_TOKEN'}
    assert values['MATCHPULSE_MODAL_WS_TOKEN'] == 'fixture-token'
    with pytest.raises(ValueError, match='both'):
        local.private_ws_credentials({'MATCHPULSE_MODAL_WS_TOKEN': 'fixture-token'}, path=credential_file)
    credential_file.write_text(json.dumps({'url': 'https://private.test?secret=never-print', 'token': 'fixture-token'}))
    with pytest.raises(ValueError) as raised:
        local.private_ws_credentials({}, path=credential_file)
    assert 'never-print' not in str(raised.value)


def test_explicit_offline_launch_keeps_server_lifecycle_unbounded_without_loading_cloud_credentials(monkeypatch):
    started = []

    class Controller:
        def __init__(self, **kwargs):
            started.append(kwargs)

        async def start(self):
            started.append('controller_owns_lifetime')

    monkeypatch.setattr(local, 'AppController', Controller)
    monkeypatch.setattr(local, 'load_local_environment', lambda: None)
    monkeypatch.setattr(local, 'private_ws_credentials',
                        lambda: (_ for _ in ()).throw(AssertionError('CPU launch must not read cloud credentials')))
    for name in ('MATCHPULSE_CAPTIONS_ENABLED', 'MATCHPULSE_ALLOWED_ORIGINS', 'MATCHPULSE_API_ALLOWED_ORIGINS'):
        monkeypatch.setenv(name, '')
    asyncio.run(local.serve(args(offline=True)))
    assert started[-1] == 'controller_owns_lifetime'
    options = started[0]
    assert options['port'] == 8030 and options['api_port'] == 8031 and options['api_enabled']
    assert options['host'] == options['api_host'] == '127.0.0.1'
    assert not options['config'].monitor
    assert options['prepared_config'] is None


def test_normal_local_launch_prepares_real_capability_without_starting_paid_monitoring(monkeypatch):
    started, credentials = [], []

    class Controller:
        def __init__(self, **kwargs):
            started.append(kwargs)

        async def start(self):
            pass

    monkeypatch.setattr(local, 'AppController', Controller)
    monkeypatch.setattr(local, 'load_local_environment', lambda: None)
    monkeypatch.setattr(local, 'private_ws_credentials', lambda: credentials.append(True) or {
        'MATCHPULSE_MODAL_WS_URL': 'wss://fixture.modal.run/session', 'MATCHPULSE_MODAL_WS_TOKEN': 'fixture-token'})
    for name in ('MATCHPULSE_MODAL_WS_URL', 'MATCHPULSE_MODAL_WS_TOKEN', 'MATCHPULSE_ALLOWED_ORIGINS',
                 'MATCHPULSE_API_ALLOWED_ORIGINS'):
        monkeypatch.setenv(name, '')
    asyncio.run(local.serve(args()))
    assert credentials == [True]
    assert not started[0]['config'].monitor
    prepared = started[0]['prepared_config']
    assert prepared.monitor and prepared.mode == 'real' and prepared.model_version == NEW_MODEL_VERSION
    assert prepared.max_duration_seconds == 2400 and prepared.max_estimated_cost_usd == 10
    assert prepared.max_restarts == 0


def test_short_bounded_validation_command_keeps_its_explicit_time_and_budget():
    _, config = local.configuration(args(allow_cloud=True, seconds=100, budget=.495), {})
    assert config.max_duration_seconds == 100 and config.max_estimated_cost_usd == .495
    assert config.mode == 'real' and config.max_restarts == 0
