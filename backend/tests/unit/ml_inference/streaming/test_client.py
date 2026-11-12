"""Offline tests for unit ml_inference streaming test_client."""


import pytest
from touchline.ml_inference.streaming import client as replay_client
from touchline.ml_inference.streaming.client import OwnedReplayHandles, ReplayClient
from tests.support.ml_inference.client import session, control, sdk_fixture, multi_session


def test_full_four_worker_window_covers_l4_and_captions_and_rejects_old_underfunded_allowance():
    from touchline.ml_inference.streaming.contract import MAX_WORKER_RATE, T4_WORKER_RATE, validate_session
    # Approved maximum resources: L4/T4, eight CPU cores, eight GiB per worker.
    l4_compute = (120 + 330) * 4 * (.000222 + 8 * .0000131 + 8 * .00000222)
    assert l4_compute == pytest.approx(.620208)
    assert MAX_WORKER_RATE >= T4_WORKER_RATE
    assert (120 + 330) * 4 * MAX_WORKER_RATE >= l4_compute
    assert (120 + 330) * 4 * MAX_WORKER_RATE + .02 <= .70
    spec = multi_session(max_duration_seconds=120, gpu_workers=4, max_estimated_cost_usd=.70)
    assert validate_session(spec) is spec
    with pytest.raises(ValueError, match="estimated compute allowance"):
        validate_session({**spec, "max_estimated_cost_usd": .60})


def test_construction_never_imports_or_authenticates_the_sdk(monkeypatch):
    def forbidden(_):
        raise AssertionError("No SDK import was authorized by construction")

    monkeypatch.setattr(replay_client.importlib, "import_module", forbidden)
    client = ReplayClient()
    assert client.sdk is None and client._dictionary is None


def test_start_requires_fresh_lease_then_uses_only_owned_app_and_compact_spec(monkeypatch):
    sdk, store, lookups, submissions, handle = sdk_fixture(monkeypatch)
    client = ReplayClient(sdk=sdk)
    client.update_control("owned-session", control())
    owned = client.start(session())
    assert isinstance(owned, OwnedReplayHandles) and owned.handles == (handle,)
    assert owned.score_keys == ('scores:owned-session',)
    assert lookups == [("matchpulse-inference", "ReplayWorker")]
    assert submissions == [session()]
    assert store.values["control:owned-session"]["session_id"] == "owned-session"
    assert "frames" not in submissions[0] and "features" not in submissions[0]


@pytest.mark.parametrize("state", [None, control(active=False), control(expires_at=1000)])
def test_missing_inactive_expired_control_never_submits_gpu_work(monkeypatch, state):
    sdk, store, lookups, submissions, _ = sdk_fixture(monkeypatch)
    if state is not None:
        store.values["control:owned-session"] = state
    with pytest.raises(RuntimeError, match="fresh viewer clock/lease"):
        ReplayClient(sdk=sdk).start(session())
    assert lookups == [] and submissions == []


def test_stop_revokes_before_cancelling_only_owned_input_and_deletes_only_its_keys(monkeypatch):
    sdk, store, _, _, handle = sdk_fixture(monkeypatch)
    client = ReplayClient(sdk=sdk)
    store.values.update({"control:owned-session": control(), "scores:owned-session": {"score": .8},
                         "control:other-session": {"untouched": True}})
    client.stop("owned-session", handle)
    assert store.events[0][0:2] == ("put", "control:owned-session")
    assert store.events[0][2]["active"] is False
    cancellation = store.events[1]
    assert cancellation[0:2] == ("cancel", {"terminate_containers": True})
    assert cancellation[2]["control:owned-session"]["active"] is False
    assert store.values == {"control:other-session": {"untouched": True}}


def test_latest_reads_only_owned_score_and_stop_without_input_is_safe(monkeypatch):
    sdk, store, _, _, _ = sdk_fixture(monkeypatch)
    store.values["scores:owned-session"] = {"worker_sequence": 5}
    client = ReplayClient(sdk=sdk)
    assert client.latest("owned-session") == {"worker_sequence": 5}
    client.stop("owned-session", None)
    client.stop("owned-session", None)
    assert store.values == {}


def test_cancellation_rpc_failure_still_removes_owned_compact_records(monkeypatch):
    sdk, store, _, _, handle = sdk_fixture(monkeypatch)
    store.values.update({"control:owned-session": control(), "scores:owned-session": {"worker_sequence": 1}})

    def failed_cancel(**kwargs):
        assert store.values["control:owned-session"]["active"] is False
        raise TimeoutError("Cancellation RPC failed")

    handle.cancel = failed_cancel
    with pytest.raises(TimeoutError):
        ReplayClient(sdk=sdk).stop("owned-session", handle)
    assert store.values == {}


@pytest.mark.parametrize("change", [
    {"mode": "mock"}, {"mode": "real", "checkpoint": None},
    {"max_duration_seconds": 3601}, {"max_estimated_cost_usd": .01},
    {"matches": session()["matches"] * 8},
    {"matches": [{"match_id": "cam-1", "source_url": "https://user:secret@media.example/video.mp4"}]},
])
def test_bad_mode_bundle_resource_or_source_contract_fails_before_sdk(monkeypatch, change):
    sdk, store, lookups, submissions, _ = sdk_fixture(monkeypatch)
    with pytest.raises(ValueError) as error:
        ReplayClient(sdk=sdk).start(session(**change))
    assert "user:secret" not in str(error.value)
    assert lookups == [] and submissions == [] and store.events == []


def test_control_identity_is_validated_before_any_sdk_mutation(monkeypatch):
    sdk, store, _, _, _ = sdk_fixture(monkeypatch)
    with pytest.raises(ValueError, match="different session"):
        ReplayClient(sdk=sdk).update_control("owned-session", control(session_id="another"))
    assert store.events == []


def test_partial_submission_failure_revokes_lease_cancels_owned_input_and_clears_keys(monkeypatch):
    sdk, store, _, specs, _ = sdk_fixture(monkeypatch)
    sdk.fail_at = 1
    client = ReplayClient(sdk=sdk)
    client.update_control('owned-session', control())
    with pytest.raises(RuntimeError, match='submission failure'):
        client.start(multi_session())
    assert len(specs) == 2 and sdk.cancelled == [0] and not store.values
    assert client._active is None
