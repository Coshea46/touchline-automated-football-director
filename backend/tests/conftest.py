"""Ordinary backend tests cannot consume a developer's local caption credits."""


import pytest


pytest_plugins = ["tests.support.captions.tracks"]


@pytest.fixture(autouse=True)
def isolated_caption_configuration(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    # Existing application fixtures deliberately use the historical public demo.
    # Private-source tests pass their own explicit environment and fake AWS SDK.
    monkeypatch.setenv("S3_BUCKET_NAME", "match-switcher-poc-videos")
    monkeypatch.setenv("MATCHPULSE_SOURCE_ACCESS", "public")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    # AppController normally loads an ignored real .env. Parser tests call the
    # original local_config function directly against their own temporary files.
    monkeypatch.setattr("touchline.main.load_local_environment", lambda: 0)
