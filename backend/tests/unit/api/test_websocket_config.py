"""Offline tests for unit api test_websocket_config."""


import pytest
from touchline.api.websocket import allowed_origins


def test_origin_allowlist_defaults_and_explicit_production_configuration(monkeypatch):
    monkeypatch.delenv("MATCHPULSE_ALLOWED_ORIGINS", raising=False)
    assert allowed_origins() == [None, "http://localhost:3000", "http://127.0.0.1:3000"]
    monkeypatch.setenv("MATCHPULSE_ALLOWED_ORIGINS", "https://matchpulse.example, https://preview.example/")
    assert allowed_origins() == [None, "https://matchpulse.example", "https://preview.example"]
    for origin in ["*", "https://user:password@example.com", "https://example.com/path", "https://example.com:bad"]:
        with pytest.raises(ValueError, match="MATCHPULSE_ALLOWED_ORIGINS"):
            allowed_origins(origin)
