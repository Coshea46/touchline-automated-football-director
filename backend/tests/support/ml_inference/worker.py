"""Reusable synthetic test doubles and builders; no provider calls."""


import importlib.util
import sys
from types import SimpleNamespace
from tests.paths import ML_ROOT


def load_app(monkeypatch):
    registrations, volumes = {}, []

    class App:
        def __init__(self, name):
            assert name == "matchpulse-inference"

        def cls(self, **kwargs):
            def decorate(cls):
                registrations[cls.__name__] = kwargs
                return cls
            return decorate

    class Volume:
        @staticmethod
        def from_name(name, **kwargs):
            volumes.append(name)
            return SimpleNamespace(reload=lambda: None)

    def asgi_app(**kwargs):
        def decorate(function):
            function.asgi_options = kwargs
            return function
        return decorate

    sdk = SimpleNamespace(
        App=App, Volume=Volume, is_local=lambda: False,
        Image=SimpleNamespace(debian_slim=lambda **kw: object()),
        parameter=lambda **kw: kw["default"], enter=lambda: lambda f: f,
        method=lambda: lambda f: f,
        asgi_app=asgi_app,
        Secret=SimpleNamespace(from_name=lambda name: SimpleNamespace(name=name)),
    )
    monkeypatch.setitem(sys.modules, "modal", sdk)
    path = ML_ROOT / "deployment" / "modal_app.py"
    spec = importlib.util.spec_from_file_location("inference_worker_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, registrations, volumes
