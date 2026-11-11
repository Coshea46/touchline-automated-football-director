"""Offline tests for unit config test_local_config."""


import pytest
from touchline.config.local import load_local_environment


def test_local_configuration_is_backend_only_literal_and_preserves_explicit_environment(tmp_path):
    path = tmp_path / ".env"
    path.write_text('FOOTBALL_DATA_API_KEY="local-token"\nGUARDIAN_API_KEY=sample # comment\n'
                    'ELEVENLABS_API_KEY=existing-file-value\nMATCHPULSE_API_PORT=8031\n'
                    'PYTHONPATH=untrusted\nexport AWS_DEFAULT_REGION=eu-west-2\n', encoding="utf-8")
    env = {"ELEVENLABS_API_KEY": "explicit"}
    assert load_local_environment(path, env) == 4
    assert env["ELEVENLABS_API_KEY"] == "explicit"
    assert env["FOOTBALL_DATA_API_KEY"] == "local-token"
    assert env["GUARDIAN_API_KEY"] == "sample"
    assert "PYTHONPATH" not in env


def test_missing_local_file_is_optional(tmp_path):
    assert load_local_environment(tmp_path / ".env", {}) == 0


def test_invalid_configuration_never_includes_the_value_in_errors(tmp_path):
    path = tmp_path / ".env"
    path.write_text('ELEVENLABS_API_KEY="do-not-disclose', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        load_local_environment(path, {})
    assert "do-not-disclose" not in str(error.value)
