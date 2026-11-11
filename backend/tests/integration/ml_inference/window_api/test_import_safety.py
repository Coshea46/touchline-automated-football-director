"""Offline tests for integration ml_inference window_api test_import_safety."""


import os
import subprocess
import sys
import pytest


@pytest.fixture(autouse=True)
def clear_ml_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("MATCHPULSE_ML_"):
            monkeypatch.delenv(key)


def test_fresh_process_import_has_no_cloud_or_model_initialization():
    code = (
        "import sys; from touchline.ml_inference import select_best_match; "
        "assert not ({'modal','matchpulse','ultralytics','torch'} & set(sys.modules))"
    )
    subprocess.run([sys.executable, "-B", "-c", code], check=True, capture_output=True, text=True)
