# Touchline backend

The installable `touchline` package owns replay sessions, the Auto Director,
WebSocket delivery, football data APIs, focused commentary captions and the
existing Modal inference integration. The Python source lives in `src/touchline`;
tests live outside the installed package in `tests`.

The package requires Python 3.11 or newer; the local setup and CI use Python 3.12.

Run these commands from the **monorepo root**, using Python 3.12:

```powershell
python -m venv backend/.venv
& backend/.venv/Scripts/python.exe -m pip install -e "./backend[dev,modal]"
& backend/.venv/Scripts/python.exe -m touchline.local --offline --no-captions
```

On Linux/macOS, use `backend/.venv/bin/python` instead. Installed console commands
include `touchline-local`, `touchline-demo`, `touchline-captions`,
`touchline-probe` and `touchline-evaluate-director`.

## Dependency environments

The CPU backend, GPU worker and caption subprocess have separate dependency
requirements. Do not install the caption requirements into the backend environment:
the supported WebSocket versions differ.

```powershell
python -m venv backend/.venv-captions
& backend/.venv-captions/Scripts/python.exe -m pip install -r backend/requirements-captions.txt
& backend/.venv-captions/Scripts/python.exe -m pip install --no-deps -e ./backend
```

The caption subprocess must have the `touchline` package installed too. Set
`MATCHPULSE_CAPTION_PYTHON` to that environment's Python executable. Docker installs
the same backend wheel into both environments, retaining their separate dependencies.
The Modal image continues to use
`src/touchline/ml_inference/deployment/requirements-worker.txt` and mounts the
complete inference package.

Live captions use `touchline.captions.live.worker` in the isolated interpreter.
The optional saved-track CLI lives under `touchline.captions.tracks.cli`;
`python -m touchline.captions` still delegates to it.

## Source layout

| Package | Responsibility |
|---|---|
| `api/` | HTTP/WebSocket transport and application routes |
| `replay/` | One owning replay session, director, sources, validated config and lifecycle coordinators |
| `captions/live/`, `captions/tracks/` | Progressive realtime captions and optional saved-track operations |
| `football_platform/` | Football provider adapters, cache/quota handling and catalog |
| `ml_inference/` | Feature extraction, model releases, streaming, deployment and complete-window compatibility API |
| `tools/` | Explicit cloud, validation, evaluation and legacy commands, with shared support helpers |

`main.py` composes the application; `local.py` and `demo.py` remain its launchers.
`paths.py` and `data/` locate writable state and packaged metadata.

`storage/` obtains temporary AWS credentials and signs private video reads. The
default bucket, reader role and setup are documented in [private S3 access](../docs/private-s3.md).
Local private playback needs an authenticated AWS profile; a Modal container uses
its injected OIDC identity. `--offline --no-captions` disables inference and
transcription while video access still requires S3 authorization.

The documented package-root imports from `touchline.replay`,
`touchline.captions` and `touchline.ml_inference` remain available, including
`SessionConfig`, `CaptionService`, `InferenceConfig`, `InferenceService` and
`select_best_match`. Internal modules moved to the groups above; use the current
module paths in [inference documentation](../docs/inference/README.md).

## Configuration and data

Copy the root `.env.example` to a gitignored root `.env` and configure only the
features you need. Neither ordinary imports nor offline tests load provider SDKs
or start cloud work. `touchline.local` loads local configuration when launched.

`src/touchline/data/matches.json` is the canonical seven-recording catalog. It is
included in the backend wheel; the frontend generates a copy before start, test
and build. Change the canonical file rather than the generated copy.

Writable artifacts live under `backend/artifacts/`, with inference artifacts under
`backend/artifacts/inference/`. Existing private inference credentials may be supplied
through `MATCHPULSE_MODAL_WS_URL` and `MATCHPULSE_MODAL_WS_TOKEN`, or through the ignored
`backend/artifacts/inference/overhaul/ws-credentials.json`. No credentials or model
weights are provisioned by installing the package.

`MATCHPULSE_PROJECT_ROOT` explicitly selects the directory containing `.env` and
`backend/artifacts/`. Editable installations detect the monorepo automatically.
Wheel installations default to their working directory; hosted environments should
set the override. All `MATCHPULSE_*` compatibility identifiers remain supported.

## Verification

```powershell
& backend/.venv/Scripts/python.exe -B -m pytest -c backend/pyproject.toml backend/tests
& backend/.venv/Scripts/python.exe -m build backend
```

`tests/unit/` mirrors application domains. `tests/integration/` exercises local
component interactions with mocked providers. Both suites run without paid services.
Reusable fake decoders, SDKs, processes and clocks live under `tests/support/`.
Tests requiring optional local model/media packages skip when those dependencies
are unavailable; cloud validation commands are separate, explicit operations.

See [architecture](../docs/architecture.md), [inference](../docs/inference/README.md)
and [scripts](../scripts/README.md) for the runtime boundaries and maintenance commands.
