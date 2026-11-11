"""Object identity uses HEAD authorization without reading private media."""

import pytest

from touchline.captions import identity as identity_module


def test_method_specific_head_presign_checks_the_get_object_identity(monkeypatch, identity):
    get_url = "https://test.invalid/match-1.mp4?method=GET&signature=GET_PRIVATE"
    head_url = "https://test.invalid/match-1.mp4?method=HEAD&signature=HEAD_PRIVATE"
    requests = []

    class HeadersOnly:
        headers = {"ETag": '"synthetic-etag"', "Content-Length": "100"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def open_head(request, timeout):
        requests.append((request.full_url, request.get_method(), timeout))
        assert request.full_url == head_url
        return HeadersOnly()

    monkeypatch.setattr(identity_module, "urlopen", open_head)
    result = identity_module.verify_source_identity("match-1.mp4", get_url, head_url)
    assert requests == [(head_url, "HEAD", 4)]
    assert result == identity["digest"]


@pytest.mark.parametrize("head_url", [
    "https://test.invalid/other.mp4?signature=PRIVATE",
    "https://other.invalid/match-1.mp4?signature=PRIVATE",
    "http://test.invalid/match-1.mp4?signature=PRIVATE",
    "https://user:PRIVATE@test.invalid/match-1.mp4",
    "https://test.invalid/match-1.mp4#PRIVATE",
])
def test_head_authorization_cannot_verify_a_different_or_invalid_locator(monkeypatch, head_url):
    def forbidden(*args, **kwargs):
        pytest.fail("A mismatched HEAD authorization must not issue a request")

    monkeypatch.setattr(identity_module, "urlopen", forbidden)
    assert identity_module.verify_source_identity(
        "match-1.mp4", "https://test.invalid/match-1.mp4?signature=GET_PRIVATE", head_url
    ) is None
