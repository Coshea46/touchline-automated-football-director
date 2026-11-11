"""Offline tests for unit replay test_config."""


import asyncio
import pytest
from touchline.replay import ReplaySession
from touchline.replay.config import SessionConfig
from tests.support.replay.session import Clock, FakeBridge, registry, configuration, playback


def test_monitoring_is_disabled_unless_server_environment_explicitly_enables_it():
    assert not SessionConfig.from_env({"MATCHPULSE_ML_MODE": "real"}).monitor
    assert SessionConfig.from_env({}).mode == "disabled"
    with pytest.raises(ValueError):
        SessionConfig.from_env({"MATCHPULSE_MONITOR": "true", "MATCHPULSE_ML_MODE": "mock"})
    # Explicit Python configurations still cannot declare a real worker without
    # specifying its artifact; only the environment factory supplies defaults.
    with pytest.raises(ValueError, match="CHECKPOINT"):
        SessionConfig(monitor=True, mode="real")


@pytest.mark.parametrize("extra", [{}, {"MATCHPULSE_ML_MODE": "real"},
                                   {"MATCHPULSE_ML_MODE": "", "MATCHPULSE_ML_CHECKPOINT": "",
                                    "MATCHPULSE_MODEL_VERSION": ""}])
def test_enabled_monitor_defaults_to_exact_selected_model_without_mock_fallback(extra):
    from touchline.ml_inference.models.release import NEW_MODEL_VERSION

    config = SessionConfig.from_env({"MATCHPULSE_MONITOR": "true", **extra})
    assert config.mode == "real" and config.monitor
    assert config.model_version == config.checkpoint == NEW_MODEL_VERSION
    assert config.director_mode == "balanced"
    assert config.mock_scores is None


def test_explicit_rollback_checkpoint_and_director_preference_are_preserved():
    from touchline.ml_inference.models.release import LEGACY_MODEL_VERSION

    config = SessionConfig.from_env({"MATCHPULSE_MONITOR": "true",
        "MATCHPULSE_ML_CHECKPOINT": LEGACY_MODEL_VERSION, "MATCHPULSE_DIRECTOR_MODE": "relaxed"})
    assert config.mode == "real"
    assert config.model_version == config.checkpoint == LEGACY_MODEL_VERSION
    assert config.director_mode == "relaxed"


def test_mock_scores_require_explicit_mock_mode_and_registered_matches():
    config = SessionConfig.from_env({"MATCHPULSE_MONITOR": "true", "MATCHPULSE_ML_MODE": "features_mock",
                                     "MATCHPULSE_MOCK_SCORES_JSON": '{"cam-1":0.1,"cam-2":0.9}'})
    assert config.mock_scores == {"cam-1": 0.1, "cam-2": 0.9}
    with pytest.raises(ValueError, match="features_mock"):
        SessionConfig(monitor=True, mode="real", checkpoint="v1/classifier.pt", mock_scores={"cam-1": 0.8})

    async def scenario():
        session = await ReplaySession(registry(), config=config, bridge=FakeBridge()).initialize()
        assert session.worker_spec()["mock_scores"]["cam-2"] == 0.9
        await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode,checkpoint", [("features_mock", None), ("real", "v1/classifier.pt")])
def test_backend_worker_spec_and_clock_validate_against_real_bridge_contract(mode, checkpoint):
    from touchline.ml_inference.streaming.contract import validate_control, validate_session

    async def scenario():
        config = SessionConfig(monitor=True, mode=mode, checkpoint=checkpoint,
                               model_version=checkpoint or "features-mock-v1")
        session = await ReplaySession(registry(), config=config, bridge=FakeBridge()).initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session, playing=False))
        assert validate_session(session.worker_spec())
        assert validate_control(session.session_id, session.control())
        await session.close()

    asyncio.run(scenario())


def test_gpu_shards_default_by_source_count_and_share_one_budget():
    async def scenario():
        session = await ReplaySession(registry(7), config=configuration(), bridge=FakeBridge()).initialize()
        from touchline.ml_inference.streaming.contract import MATCHES_PER_GPU

        assert session.worker_spec()["gpu_workers"] == (7 + MATCHES_PER_GPU - 1) // MATCHES_PER_GPU
        await session.close()
        session = await ReplaySession(registry(7), config=configuration(gpu_workers=1),
                                       bridge=FakeBridge()).initialize()
        assert session.worker_spec()["gpu_workers"] == 1
        await session.close()
        bridge = FakeBridge()
        session = await ReplaySession(registry(7), config=configuration(max_estimated_cost_usd=0.2),
                                       bridge=bridge).initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session))
        await session._control_tick()
        assert session.error_code == "session_budget_reached"
        assert not bridge.calls
        await session.close()

    asyncio.run(scenario())
    assert SessionConfig.from_env({"MATCHPULSE_GPU_WORKERS": ""}).gpu_workers is None
    with pytest.raises(ValueError):
        configuration(gpu_workers=5)


def test_four_worker_allocation_reserves_every_resource_against_one_allowance():
    from touchline.ml_inference.streaming.contract import MAX_WORKER_RATE, validate_session

    async def scenario():
        clock, bridge = Clock(), FakeBridge()
        session = await ReplaySession(registry(7), config=configuration(max_estimated_cost_usd=2),
                                       bridge=bridge, now=clock, monotonic=clock).initialize()
        session.connect("viewer")
        session.receive("viewer", playback(session))
        await session._control_tick()
        spec = next(call[1] for call in bridge.calls if call[0] == "start")
        assert spec["gpu_workers"] == 4 and len(spec["matches"]) == 7
        assert validate_session(spec)
        assert session.metrics["reserved_compute_usd"] == pytest.approx((300 + 330) * MAX_WORKER_RATE * 4)
        assert session.worker_spec()["max_estimated_cost_usd"] == pytest.approx(
            2 - session.metrics["reserved_compute_usd"])
        await session.close()
        assert session.metrics["worker_stop_succeeded"]

    asyncio.run(scenario())
