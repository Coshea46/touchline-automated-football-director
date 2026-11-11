"""Offline tests for unit football_platform test_transport."""


from urllib.parse import parse_qs, urlsplit
import pytest
from touchline.football_platform.providers import JSONTransport, ProviderError


def test_provider_auth_fixed_hosts_no_redirect_and_bounded_read(monkeypatch):
    from touchline.football_platform import providers

    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, bound):
            assert bound == JSONTransport.MAX_BYTES + 1
            return b'{"response":{"status":"ok","results":[]}}'

    class Opener:
        def open(self, req, timeout):
            assert timeout == 6
            captured.append(req)
            return Response()

    def opener(*handlers):
        assert handlers[0].redirect_request(None, None, 302, None, {}, "https://attacker.example") is None
        return Opener()

    monkeypatch.setattr(providers.request, "build_opener", opener)
    transport = JSONTransport()
    transport.get("guardian", "search", {"api-key": "test-only-guardian", "q": "Chelsea & Arsenal"}, {})
    parts = urlsplit(captured[0].full_url)
    assert parts.hostname == "content.guardianapis.com"
    assert parse_qs(parts.query)["q"] == ["Chelsea & Arsenal"]
    transport.get("football-data.org", "competitions/PL/standings", {}, {"X-Auth-Token": "test-only-football"})
    assert captured[1].get_header("X-auth-token") == "test-only-football"
    with pytest.raises(ProviderError):
        transport.get("attacker", "search", {}, {})
