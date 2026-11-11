"""Offline tests for unit captions tracks test_cli."""


import json
from types import SimpleNamespace
import pytest
from touchline.captions.tracks.pipeline import CaptionPipeline
from touchline.captions.errors import CaptionError
from tests.support.captions.tracks import FakeTranscriber, generate_args


def test_caption_cli_loads_local_configuration_and_checks_identity_without_provider_key(monkeypatch, capsys, tmp_path, identity):
    from touchline.captions.tracks import cli

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    loaded, heads = [], []
    monkeypatch.setattr(cli, "load_local_environment", lambda: loaded.append(True))
    source = SimpleNamespace(match_id="cam-1", key="match-1.mp4", source_url="https://test.invalid/match-1.mp4")
    monkeypatch.setattr(cli, "SourceRegistry", SimpleNamespace(from_env=lambda: SimpleNamespace(resolve=lambda: [source])))
    def head(value):
        heads.append(value)
        return identity
    monkeypatch.setattr(cli, "head_identity", head)
    assert cli.main(["--match-id", "cam-1", "--execute", "--track-dir", str(tmp_path)]) == 2
    assert loaded == [True]
    assert heads == [source]
    assert not (tmp_path / "ledger.json").exists()
    assert json.loads(capsys.readouterr().out) == {"status": "unavailable", "reason": "api_key_missing"}


def test_cli_completed_reuse_verifies_source_without_transcription_key(tmp_path, identity, monkeypatch, capsys):
    from touchline.captions.tracks import cli

    CaptionPipeline(tmp_path, transcriber=FakeTranscriber(), decoder=lambda *a, **k: (b"fixture-wav", {})).generate(
        **generate_args(identity), execute=True)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(cli, "load_local_environment", lambda: None)
    source = SimpleNamespace(match_id="cam-1", key="match-1.mp4", source_url="https://test.invalid/match-1.mp4")
    monkeypatch.setattr(cli, "SourceRegistry", SimpleNamespace(from_env=lambda: SimpleNamespace(resolve=lambda: [source])))
    heads = []
    def head(value):
        heads.append(value)
        return identity
    monkeypatch.setattr(cli, "head_identity", head)
    assert cli.main(["--match-id", "cam-1", "--execute", "--track-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "reused" and heads == [source]


def test_realtime_access_cli_emits_only_sanitized_result(monkeypatch, capsys):
    from touchline.captions.tracks import cli

    monkeypatch.setattr(cli, "load_local_environment", lambda: None)
    monkeypatch.setattr(cli, "SourceRegistry", SimpleNamespace(from_env=lambda: pytest.fail("credential check must not resolve media")))
    class Provider:
        def realtime_access_check(self):
            return {"status": "available", "transcription_started": False}
    monkeypatch.setattr(cli, "ElevenLabsTranscriber", Provider)
    assert cli.main(["--realtime-access-check"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "available", "transcription_started": False}
    class MissingPermission:
        def realtime_access_check(self):
            raise CaptionError("provider_permission_speech_to_text_missing")
    monkeypatch.setattr(cli, "ElevenLabsTranscriber", MissingPermission)
    assert cli.main(["--realtime-access-check"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "unavailable", "reason": "provider_permission_speech_to_text_missing"}
