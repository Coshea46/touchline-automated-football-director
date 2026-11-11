# Backend tests

The suite uses synthetic providers and explicit local fixtures. It does not start
Modal jobs, transcribe paid audio, fetch football providers or read local API keys.
The root `conftest.py` removes the caption key and prevents application startup
from loading the developer's `.env`.

## Layout

- `unit/` follows the application packages. Parsers, configuration, director
  rules and orchestration use injected boundaries rather than real sockets or
  subprocesses.
- `integration/` covers loopback HTTP/WebSockets, local subprocesses, caption
  track file workflows and native audio decoder parity. Directory membership
  automatically applies the `integration` marker.
- `support/` owns reusable synthetic clocks, providers, transports, model
  adapters and session builders. Tests import helpers from here, never from
  another `test_*.py` module. The shared caption track fixtures are registered
  through the root `conftest.py`.
- `fixtures/ml_inference/` contains the audited, small PCM/features/checkpoint
  fixtures. Production runtime assets remain in the installed inference package.
- `paths.py` resolves fixture and installed-package locations independently of
  how deeply a test is nested.

## Run

From the repository root, after installing the backend development dependencies:

```powershell
& backend/.venv/Scripts/python.exe -B -m pytest -c backend/pyproject.toml backend/tests
```

Add `-m "not integration"` for the unit suite or `-m integration` for the local
integration suite. Both are provider-free. Native audio tests use the development
environment's PyAV, SciPy and OpenCV dependencies; the real selected-model loading
check additionally needs optional Torch and skips explicitly when it is absent.

On Linux/macOS, use `backend/.venv/bin/python`. If a restricted environment cannot
access its default pytest temporary directory, create `backend/artifacts/` and
pass a fresh `--basetemp backend/artifacts/test-tmp-<run>` directory.
