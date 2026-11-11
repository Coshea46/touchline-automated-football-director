"""Offline tests for unit config test_captions."""


import asyncio
import pytest
from touchline.main import AppController
from touchline.replay.config import SessionConfig
from tests.support.replay.session import playback
from tests.support.captions.session import CaptionWorker


@pytest.mark.parametrize("settings", [{"MATCHPULSE_CAPTIONS_ENABLED": "invalid"},
                                     {"MATCHPULSE_CAPTION_MAX_AUDIO_SECONDS": "28801"},
                                     {"MATCHPULSE_CAPTION_BUDGET_USD": "3.601"}])
def test_main_live_feature_flag_and_authorized_caps_are_strict(monkeypatch, settings):
    monkeypatch.setattr("touchline.main.load_local_environment", lambda: 0)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        AppController(config=SessionConfig(), api_enabled=False)


@pytest.mark.parametrize("obsolete_flag", [None, "false", "invalid"])
def test_api_key_alone_configures_default_on_without_any_authorization_flag(monkeypatch, obsolete_flag):
    monkeypatch.setattr("touchline.main.load_local_environment", lambda: 0)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "TEST-ONLY-NEVER-CALL")
    if obsolete_flag is None:
        monkeypatch.delenv("MATCHPULSE_CAPTION_LIVE_AUTHORIZED", raising=False)
    else:
        monkeypatch.setenv("MATCHPULSE_CAPTION_LIVE_AUTHORIZED", obsolete_flag)
    worker = CaptionWorker()
    monkeypatch.setattr("touchline.captions.live.adapter.RealtimeCaptionWorker", lambda *args, **kwargs: worker)

    async def check():
        controller = AppController(config=SessionConfig(), api_enabled=False, host="127.0.0.1", port=0)
        task = asyncio.create_task(controller.start())
        try:
            for _ in range(100):
                if controller.ws_manager.server is not None:
                    break
                await asyncio.sleep(.01)
            state = controller.session.caption_state()
            assert state["configured"] and state["available"] and state["user_enabled"] and state["enabled"]
            assert "authorized" not in state and state["deadline"] is None
            assert not worker.updates
            controller.session.connect("viewer")
            controller.session.receive("viewer", playback(controller.session, position=5))
            assert worker.updates and worker.updates[-1]["position"] == 5
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())


@pytest.mark.parametrize("configured,feature_enabled,reason", [(False, True, "missing_key"),
                                                             (True, False, "feature_disabled")])
def test_missing_key_and_operational_kill_switch_keep_useful_default_on_intent(
        monkeypatch, configured, feature_enabled, reason):
    monkeypatch.setattr("touchline.main.load_local_environment", lambda: 0)
    if configured:
        monkeypatch.setenv("ELEVENLABS_API_KEY", "TEST-ONLY-NEVER-CALL")
    monkeypatch.setenv("MATCHPULSE_CAPTIONS_ENABLED", str(feature_enabled).lower())
    controller = AppController(config=SessionConfig(), api_enabled=False)
    state = controller.session.caption_state()
    assert state["user_enabled"] is True and state["enabled"] is False
    assert state["reason"] == reason and state["configured"] is configured
    assert state["deadline"] is None and controller.session.caption_worker is None
