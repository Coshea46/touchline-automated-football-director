"""Reusable synthetic test doubles and builders; no provider calls."""


from types import SimpleNamespace
import numpy as np


class Handle:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error
        self.cancelled = []

    def get(self, timeout):
        self.timeout = timeout
        if self.error:
            raise self.error
        return self.result

    def cancel(self, **kwargs):
        self.cancelled.append(kwargs)


class Method:
    def __init__(self, function):
        self.function = function

    def spawn(self, *args):
        return self.function(*args)


def sdk_fixture(feature=None, ready=None, predict=None):
    lookups, options, parameters = [], [], []
    feature = feature or (lambda payload: Handle(np.zeros((75, 215), np.float32)))
    ready = ready or (lambda: Handle({"schema": "matchpulse-215-v1", "input_shape": [75, 215], "audio_required": True, "model_input_size": 215}))
    predict = predict or (lambda f: Handle({key: 0.5 for key in f}))

    class Worker:
        def __init__(self, name):
            self.name = name

        def with_options(self, **kwargs):
            options.append(kwargs)
            return self

        def __call__(self, **kwargs):
            parameters.append(kwargs)
            return SimpleNamespace(extract=Method(feature), ready=Method(ready), predict=Method(predict))

    class Cls:
        @staticmethod
        def from_name(app, name):
            assert app == "matchpulse-inference"
            lookups.append((app, name))
            return Worker(name)

    sdk = SimpleNamespace(Cls=Cls, exception=SimpleNamespace(TimeoutError=TimeoutError,
                                                           FunctionTimeoutError=TimeoutError))
    return sdk, lookups, options, parameters
