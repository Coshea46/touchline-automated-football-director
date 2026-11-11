"""Offline tests for unit config test_application."""


from touchline.main import AppController
from touchline.replay.config import SessionConfig
from tests.support.replay.session import registry


def test_app_default_startup_has_no_legacy_rgb_ingester():
    controller = AppController(registry=registry(), config=SessionConfig(), host="127.0.0.1", port=0)
    assert not hasattr(controller, "ingester")
    assert not controller.session.config.monitor
