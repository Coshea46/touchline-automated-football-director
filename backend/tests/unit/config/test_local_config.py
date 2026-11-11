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


def test_private_video_bucket_and_profile_are_loaded_without_overwriting_shell_settings(tmp_path):
    path = tmp_path / ".env"
    path.write_text("S3_BUCKET_NAME=private-recordings\nAWS_PROFILE=touchline-local\n"
                    "AWS_ROLE_ARN=arn:aws:iam::709860105019:role/TouchlineVideoReader\n"
                    "MATCHPULSE_SOURCE_ACCESS=presigned\n", encoding="utf-8")
    env = {"AWS_PROFILE": "already-selected"}
    assert load_local_environment(path, env) == 3
    assert env["S3_BUCKET_NAME"] == "private-recordings"
    assert env["AWS_PROFILE"] == "already-selected"
    assert env["MATCHPULSE_SOURCE_ACCESS"] == "presigned"


def test_invalid_configuration_never_includes_the_value_in_errors(tmp_path):
    path = tmp_path / ".env"
    path.write_text('ELEVENLABS_API_KEY="do-not-disclose', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        load_local_environment(path, {})
    assert "do-not-disclose" not in str(error.value)
