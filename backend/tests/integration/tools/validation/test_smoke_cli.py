"""Offline tests for integration tools validation test_smoke_cli."""


import subprocess
import sys


def test_cloud_smoke_requires_explicit_opt_in():
    result = subprocess.run([sys.executable, "-B", "-m", "touchline.tools.validation.smoke"],
                            check=False, capture_output=True, text=True)
    assert result.returncode == 2 and "Explicit approval" in result.stderr
