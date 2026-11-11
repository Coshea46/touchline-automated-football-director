"""Offline tests for unit tools legacy test_window_validation."""


import sys
from fractions import Fraction
from types import SimpleNamespace
import numpy as np
import pytest
from touchline.tools.legacy import window_validation as validation


def test_deploy_keeps_worker_module_importable_without_automatic_source_mount(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "ARTIFACTS", tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(validation.subprocess, "run", run)
    validation.deploy()
    command, options = calls[0]
    assert command[-3:] == ["deploy", "-m", "touchline.ml_inference.deployment.modal_app"]
    assert options["env"]["MATCHPULSE_ML_WORKER_CONCURRENCY"] == "1"
    assert options["env"]["MATCHPULSE_ML_GPU"] == "T4"
    assert validation.TaskBudget().data["estimated_usd"] == 0.6


def test_task_budget_refuses_more_than_five_dollars(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "ARTIFACTS", tmp_path)
    budget = validation.TaskBudget()
    row = budget.reserve("bounded", 4.9)
    with pytest.raises(RuntimeError, match="exhausted"):
        budget.reserve("overrun", 0.2)
    budget.settle(row, 1)
    assert budget.data["estimated_usd"] < 4.9
    assert validation.TaskBudget().data == budget.data


@pytest.mark.parametrize("amount", [0, -1, float("nan"), float("inf")])
def test_bad_reservations(tmp_path, monkeypatch, amount):
    monkeypatch.setattr(validation, "ARTIFACTS", tmp_path)
    with pytest.raises(ValueError):
        validation.TaskBudget().reserve("bad", amount)


def test_pts_preparation_uses_displayed_frame_at_each_interval_centre(tmp_path, monkeypatch):
    path = tmp_path / "fixture.mp4"
    path.write_bytes(b"fake source handled by injected decoder")

    class Frame:
        is_corrupt = False
        duration = 2

        def __init__(self, index):
            self.pts, self.index = index * 2, index

        def to_ndarray(self, *, format):
            assert format == "rgb24"
            return np.full((16, 16, 3), self.index, np.uint8)

    class Container:
        streams = SimpleNamespace(video=[SimpleNamespace(
            start_time=0, time_base=Fraction(1, 30), duration=150, width=16, height=16
        )])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def decode(self, stream):
            return iter(Frame(i) for i in range(75))

    monkeypatch.setitem(sys.modules, "av", SimpleNamespace(open=lambda path: Container()))
    frames, metadata = validation.load_football_window(path, 0)
    np.testing.assert_array_equal(frames[:, 0, 0, 0], np.arange(75))
    assert metadata["target_timestamps"][0] == pytest.approx(1 / 30)
    assert metadata["target_timestamps"][-1] == pytest.approx(5 - 1 / 30)
    assert len(metadata["source_timestamps"]) == 75
