"""Offline tests for unit api test_http_config."""


import pytest
from touchline.api.http import HTTPServer


@pytest.mark.parametrize("origin", ["*", "https://example.com/path", "http://user@example.com",
                                     "http://example.com?query=1", "http://example.com\r\nInjected: yes"])
def test_invalid_api_origin_configuration_rejected(origin):
    with pytest.raises(ValueError, match="invalid_api_origins"):
        HTTPServer(allowed_origins=[origin])
