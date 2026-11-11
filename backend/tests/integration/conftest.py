"""Integration tests stay offline and can be selected independently."""


from pathlib import Path
import pytest


def pytest_collection_modifyitems(items):
    directory = Path(__file__).resolve().parent
    for item in items:
        if directory in Path(item.path).resolve().parents:
            item.add_marker(pytest.mark.integration)
