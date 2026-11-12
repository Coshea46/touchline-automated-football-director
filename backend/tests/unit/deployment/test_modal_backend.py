import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import touchline
import pytest

from touchline.deployment.image import ADMISSION_DATABASE, CAPTION_PYTHON, STATE_DIRECTORY


class Image:
    def __init__(self):
        self.calls = []

    @classmethod
    def debian_slim(cls, **kwargs):
        image = cls()
        image.calls.append(("debian_slim", (), kwargs))
        return image

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self
        return call


class Volume:
    def __init__(self, name, create_if_missing):
        self.name = name
        self.create_if_missing = create_if_missing
        self.events = []

    def reload(self):
        self.events.append("reload")

    def commit(self):
        self.events.append("commit")

    @classmethod
    def from_name(cls, name, *, create_if_missing):
        return cls(name, create_if_missing)


class App:
    def __init__(self, name):
        self.name = name

    def function(self, **kwargs):
        def decorate(function):
            function.function_options = kwargs
            return function
        return decorate


def load_module(monkeypatch, *, local=True):
    def decorator(attribute):
        def configure(**kwargs):
            def decorate(function):
                setattr(function, attribute, kwargs)
                return function
            return decorate
        return configure

    fake = SimpleNamespace(
        Image=Image, App=App, Volume=Volume,
        Secret=SimpleNamespace(from_name=lambda name: SimpleNamespace(name=name)),
        is_local=lambda: local,
        concurrent=decorator("concurrency_options"),
        asgi_app=decorator("asgi_options"),
    )
    monkeypatch.setitem(sys.modules, "modal", fake)
    monkeypatch.delenv("MATCHPULSE_BACKEND_SECRET_NAME", raising=False)
    monkeypatch.delenv("MATCHPULSE_MODAL_WS_TOKEN", raising=False)
    monkeypatch.delenv("MATCHPULSE_REPLAY_WS_TOKEN", raising=False)
    path = Path(touchline.__file__).parent / "deployment" / "modal_backend.py"
    specification = importlib.util.spec_from_file_location("offline_cpu_deployment", path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_public_cpu_function_keeps_one_warm_memory_owner(monkeypatch):
    module = load_module(monkeypatch)
    options = module.public_backend.function_options
    assert module.app.name == "touchline-backend"
    assert options["min_containers"] == 1 and options["buffer_containers"] == 0
    assert options["max_containers"] == 1
    assert options["cpu"] == (0.25, 4) and options["memory"] == (512, 4096)
    assert options["scaledown_window"] == 60
    assert options["timeout"] >= 1200 + 60
    assert "retries" not in options  # Web functions reject any explicit retry setting.
    assert options["routing_region"] == "eu-west"
    assert "gpu" not in options and "region" not in options
    assert module.public_backend.concurrency_options["max_inputs"] > 10
    assert module.public_backend.asgi_options == {"label": "touchline-backend"}
    assert options["include_source"] is False
    assert [secret.name for secret in options["secrets"]] == [
        "touchline-backend-config", "matchpulse-inference-replay-auth"]
    assert options["volumes"] == {STATE_DIRECTORY: module.state_volume}
    assert not module.state_volume.create_if_missing


def test_image_uses_explicit_source_inputs_and_the_same_wheel_in_isolated_captions(monkeypatch):
    module = load_module(monkeypatch)
    calls = module.image.calls
    files = [(Path(args[0]).name, args[1], kwargs) for name, args, kwargs in calls if name == "add_local_file"]
    assert {name for name, _, _ in files} == {"pyproject.toml", "README.md", "requirements-captions.txt"}
    assert all(kwargs["copy"] for _, _, kwargs in files)
    directories = [(args, kwargs) for name, args, kwargs in calls if name == "add_local_dir"]
    assert len(directories) == 1
    assert Path(directories[0][0][0]).name == "src"
    assert directories[0][1]["copy"]
    assert "**/.env" in directories[0][1]["ignore"]
    commands = next(args for name, args, _ in calls if name == "run_commands")
    assert any("pip wheel --no-deps" in command for command in commands)
    assert "python -m pip install --no-cache-dir /opt/touchline-wheels/touchline_backend-*.whl" in commands
    assert CAPTION_PYTHON + " -m pip install --no-deps /opt/touchline-wheels/touchline_backend-*.whl" in commands
    assert all("requirements-captions.txt" not in command or command.startswith(CAPTION_PYTHON) for command in commands)
    environment = next(args[0] for name, args, _ in calls if name == "env")
    assert environment["MATCHPULSE_CAPTION_PYTHON"] == CAPTION_PYTHON
    assert environment["MATCHPULSE_MONITOR"] == environment["MATCHPULSE_CAPTIONS_ENABLED"] == "false"
    assert not any("KEY" in name or "TOKEN" in name or "SECRET" in name for name in environment)


@pytest.mark.parametrize("existing_token", [None, "EXPLICIT_PRIVATE_TEST_TOKEN"])
def test_asgi_factory_reloads_state_and_maps_only_missing_server_token(monkeypatch, existing_token, capsys):
    module = load_module(monkeypatch)
    monkeypatch.setenv("MATCHPULSE_REPLAY_WS_TOKEN", "EXISTING_INFERENCE_PRIVATE_TEST_TOKEN")
    if existing_token is not None:
        monkeypatch.setenv("MATCHPULSE_MODAL_WS_TOKEN", existing_token)
    package = ModuleType("touchline.hosted")
    package.__path__ = []
    admission_module = ModuleType("touchline.hosted.admission")
    app_module = ModuleType("touchline.hosted.app")
    captured = {}

    class Policy:
        def __init__(self, **kwargs):
            assert module.state_volume.events == ["reload"]
            captured.update(kwargs)

    application = object()

    def create_app(**kwargs):
        assert isinstance(kwargs["admission"], Policy)
        assert module.os.environ["MATCHPULSE_MODAL_WS_TOKEN"] == (
            existing_token or "EXISTING_INFERENCE_PRIVATE_TEST_TOKEN")
        return application

    admission_module.AdmissionPolicy = Policy
    app_module.create_app = create_app
    for name, replacement in (("touchline.hosted", package), ("touchline.hosted.admission", admission_module),
                              ("touchline.hosted.app", app_module)):
        monkeypatch.setitem(sys.modules, name, replacement)
    assert module.public_backend() is application
    assert captured["db_path"] == ADMISSION_DATABASE
    assert captured["commit"] == module.state_volume.commit
    assert captured["env"] is module.os.environ
    assert module.state_volume.events == ["reload"]
    output = capsys.readouterr()
    assert not output.out and not output.err


def test_remote_import_does_not_require_local_checkout_build_inputs(monkeypatch):
    module = load_module(monkeypatch, local=False)
    assert [name for name, _, _ in module.image.calls] == ["debian_slim"]
    assert module.state_volume.events == []
