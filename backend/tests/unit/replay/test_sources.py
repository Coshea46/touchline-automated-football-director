"""Offline tests for unit replay test_sources."""


import pytest
from touchline.replay import MatchSource, SourceRegistry
from tests.support.replay.session import Clock


def test_explicit_public_demo_registry_is_seven_encoded_media_sources():
    sources = SourceRegistry.from_env({'S3_BUCKET_NAME': 'match-switcher-poc-videos', 'MATCHPULSE_SOURCE_ACCESS': 'public'}).resolve()
    assert len(sources) == 7
    assert sources[-1].worker_spec()["source_url"].endswith("/match-7.mp4")
    assert all(source.offset_seconds == 0 for source in sources)


def test_registry_presigns_refreshes_and_never_exposes_urls_in_repr():
    clock = Clock()

    class S3:
        def __init__(self):
            self.calls = []

        def generate_presigned_url(self, method, **kwargs):
            self.calls.append((method, kwargs))
            return f"https://example.com/media?signature=secret-{len(self.calls)}"

        def head_object(self, **kwargs):
            assert kwargs == {"Bucket": "match-switcher-poc-videos", "Key": "a b.mp4"}
            return {"ETag": '"offline-private-object"', "ContentLength": 100}

    s3 = S3()
    sources = SourceRegistry([MatchSource("cam-1", "Private", key="a b.mp4")],
                             access="presigned", url_ttl_seconds=120, s3_client=s3, now=clock)
    first = sources.resolve()[0]
    assert "secret" not in repr(first)
    assert first.playback_url == first.source_url
    assert first.head_url != first.source_url
    assert [call[0] for call in s3.calls] == ["get_object", "head_object"]
    assert s3.calls[0][1]["Params"]["Key"] == "a b.mp4"
    assert not sources.needs_refresh()
    clock.value += 61
    assert sources.needs_refresh()
    assert first.source_url != sources.resolve()[0].source_url


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "3"])
def test_invalid_offsets_rejected(value):
    with pytest.raises(ValueError):
        MatchSource("cam-1", "Match", key="one", offset_seconds=value)


@pytest.mark.parametrize("source", ["file:///secret", "http://example.com/a", "https://user:pass@example.com/a"])
def test_invalid_source_protocols_rejected_without_echoing_source(source):
    with pytest.raises(ValueError) as error:
        MatchSource("cam-1", "Match", source_url=source)
    assert source not in str(error.value)


def test_source_configuration_offsets_and_bounded_registration():
    configured = SourceRegistry.from_env({'S3_BUCKET_NAME': 'match-switcher-poc-videos', 'MATCHPULSE_SOURCE_ACCESS': 'public', "MATCHPULSE_MATCH_COUNT": "2",
                                          "MATCHPULSE_OFFSETS_JSON": '{"cam-2": 42.5}'}).resolve()
    assert configured[1].offset_seconds == 42.5
    with pytest.raises(ValueError, match="unique"):
        SourceRegistry([configured[0], configured[0]])
    with pytest.raises(ValueError):
        SourceRegistry.from_env({'S3_BUCKET_NAME': 'match-switcher-poc-videos', 'MATCHPULSE_SOURCE_ACCESS': 'public', "MATCHPULSE_MATCH_COUNT": "8"})
    with pytest.raises(ValueError):
        SourceRegistry.from_env({'S3_BUCKET_NAME': 'match-switcher-poc-videos', 'MATCHPULSE_SOURCE_ACCESS': 'public', "MATCHPULSE_OFFSETS_JSON": '{"unknown":1}'})
