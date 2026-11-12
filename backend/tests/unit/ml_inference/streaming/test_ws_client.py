"""Transport failure diagnostics remain useful without disclosing wire secrets."""

import hashlib
from http import HTTPStatus
import logging
import math
import time
from types import SimpleNamespace

import pytest

from touchline.ml_inference.streaming.ws_client import ReplayWebSocketClient
from tests.support.ml_inference.client import session


SECRET = 'fixture-header-secret'
SIGNED_URL = 'https://recordings.invalid/match.mp4?X-Amz-Signature=fixture-private'
LOGGER = 'touchline.ml_inference.streaming.ws_client'


class FixtureTransportError(RuntimeError):
    def __init__(self, status=None, *, response=False):
        super().__init__(SIGNED_URL + ' Authorization: Bearer ' + SECRET)
        self.str_calls = 0
        if response:
            self.response = SimpleNamespace(status_code=status)
        else:
            self.status_code = status

    def __str__(self):
        self.str_calls += 1
        return super().__str__()


def fail_transport(phase, error, caplog):
    """Drive the actual owned transport thread with an entirely fake socket."""
    options = []

    class Socket:
        async def send(self, raw):
            if phase == 'send_start':
                raise error

        async def recv(self):
            raise error

    class Context:
        async def __aenter__(self):
            if phase == 'connect':
                raise error
            return Socket()

        async def __aexit__(self, *_):
            return False

    def connector(url, **kwargs):
        options.append((url, kwargs))
        return Context()

    caplog.set_level(logging.WARNING, logger=LOGGER)
    client = ReplayWebSocketClient('ws://127.0.0.1:1/session', SECRET,
                                  allow_loopback=True, connector=connector)
    client.update_control('owned-session', {'position': 0, 'playing': True, 'active': True,
        'expires_at': time.time() + 5, 'clock_epoch': 0})
    owned = client.start(session(matches=[{'match_id': 'cam-1', 'source_url': SIGNED_URL,
                                         'offset_seconds': 0}]))
    try:
        assert owned.handles[0]._done.wait(2)
        snapshot = client.latest('owned-session')
        assert snapshot['state'] == 'error' and snapshot['error_type'] == type(error).__name__
        records = [record for record in caplog.records if record.name == LOGGER]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.WARNING and record.exc_info is None
        assert record.args[0] == phase
        assert isinstance(record.args[1], float) and math.isfinite(record.args[1]) and record.args[1] >= 0
        assert record.args[2] == type(error).__name__
        assert record.args[4] == hashlib.sha256(b'owned-session').hexdigest()
        assert 'owned-session' not in record.getMessage()
        assert SECRET not in repr(record.__dict__) and SIGNED_URL not in repr(record.__dict__)
        assert SECRET not in caplog.text and 'X-Amz-Signature' not in caplog.text
        assert error.str_calls == 0
        # Diagnostic logging must not change authentication or existing bounds.
        assert options[0][1]['extra_headers'] == {
            'Authorization': 'Bearer ' + SECRET, 'X-MatchPulse-Replay-Token': SECRET}
        assert options[0][1]['open_timeout'] == 90 and options[0][1]['close_timeout'] == .5
        return record
    finally:
        with pytest.raises(RuntimeError, match='TimeoutError'):
            client.stop('owned-session', owned)
        assert not client._local._values


@pytest.mark.parametrize('phase', ['connect', 'send_start', 'stream'])
def test_failure_warning_records_transport_phase_class_elapsed_and_session_hash_without_secrets(phase, caplog):
    record = fail_transport(phase, FixtureTransportError(HTTPStatus.FORBIDDEN), caplog)
    assert record.args[3] == 403 and 'http_status=403' in record.getMessage()


@pytest.mark.parametrize('status', [None, True, 99, 600, '403', 403.0, SIGNED_URL])
def test_only_bounded_integer_http_status_is_logged(status, caplog):
    record = fail_transport('connect', FixtureTransportError(status), caplog)
    assert record.args[3] is None and 'http_status=None' in record.getMessage()


def test_response_status_is_supported_without_logging_the_response_or_exception(caplog):
    record = fail_transport('connect', FixtureTransportError(429, response=True), caplog)
    assert record.args[3] == 429


def test_environment_can_extend_only_connection_deadline_to_110_without_changing_auth_or_other_limits(monkeypatch):
    monkeypatch.setenv('MATCHPULSE_MODAL_WS_URL', 'https://fixture.modal.run')
    monkeypatch.setenv('MATCHPULSE_MODAL_WS_TOKEN', SECRET)
    monkeypatch.delenv('MATCHPULSE_MODAL_WS_CONNECT_TIMEOUT', raising=False)
    assert ReplayWebSocketClient.from_env().connect_timeout == 90
    monkeypatch.setenv('MATCHPULSE_MODAL_WS_CONNECT_TIMEOUT', '110')
    client = ReplayWebSocketClient.from_env()
    assert client.connect_timeout == 110 and client._token == SECRET
    assert client._url == 'wss://fixture.modal.run/session'
    assert (client.stop_timeout, client.send_timeout, client.read_timeout, client.control_interval) == (4, 2, 15, .25)


@pytest.mark.parametrize('value', [True, False, None, '110', 0, -1, 110.001, float('nan'), float('inf'), -float('inf')])
def test_constructor_rejects_invalid_connection_deadlines(value):
    with pytest.raises(ValueError, match='connection deadline'):
        ReplayWebSocketClient('https://fixture.modal.run', SECRET, connect_timeout=value)


@pytest.mark.parametrize('value', ['110.001', '111', 'nan', 'inf', '-inf', 'true', 'false', '', '0', '-1'])
def test_environment_rejects_invalid_connection_deadlines(value, monkeypatch):
    monkeypatch.setenv('MATCHPULSE_MODAL_WS_URL', 'https://fixture.modal.run')
    monkeypatch.setenv('MATCHPULSE_MODAL_WS_TOKEN', SECRET)
    monkeypatch.setenv('MATCHPULSE_MODAL_WS_CONNECT_TIMEOUT', value)
    with pytest.raises(ValueError, match='connection deadline'):
        ReplayWebSocketClient.from_env()


@pytest.mark.parametrize('option,value', [('stop_timeout', 5.01), ('send_timeout', 5.01),
    ('read_timeout', 30.01), ('control_interval', .251), ('read_timeout', 110), ('stop_timeout', float('nan'))])
def test_longer_connection_deadline_does_not_relax_stream_control_or_shutdown_limits(option, value):
    with pytest.raises(ValueError, match='bounded'):
        ReplayWebSocketClient('https://fixture.modal.run', SECRET, connect_timeout=110, **{option: value})
