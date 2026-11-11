# Maintenance scripts and installed commands

Run these examples from the repository root after [backend installation](../backend/README.md).
On Linux/macOS, replace `backend/.venv/Scripts/` with `backend/.venv/bin/` and omit
Windows executable suffixes.

## Fixture catalogue sync

```powershell
node scripts/sync-match-catalog.cjs
```

This copies `backend/src/touchline/data/matches.json` into the generated, ignored
`frontend/src/data/matches.json`. It preserves the exact source bytes and resolves
paths from the script location, independently of the caller's working directory.
A missing canonical file fails the command. The frontend's `prestart`, `pretest`
and `prebuild` lifecycle hooks call it automatically. Edit the backend catalogue
rather than its generated frontend copy.

## Offline package verification

```powershell
& backend/.venv/Scripts/python.exe -m build backend
& backend/.venv/Scripts/python.exe -B scripts/verify_backend_package.py backend/dist/touchline_backend-0.1.0-py3-none-any.whl
```

Use the filename produced by the build if the package version changes. The
verifier checks wheel contents, installs the wheel into a temporary target with
`--no-deps --no-index`, then verifies package imports, console entry points,
seven-source metadata, audited release support and caption IPC startup on closed
input. It strips provider settings from the child environment and makes no
provider requests. The development interpreter supplies the verifier's already
installed dependencies; this check does not test live transcription or inference.

After installing the separate caption environment, add
`--caption-python backend/.venv-captions/Scripts/python.exe` to also verify its
ElevenLabs SDK, isolated WebSocket version and subprocess startup on closed input.

## Installed Python commands

Application and operational entry points are installed by `backend/pyproject.toml`:

| Command | Module | Role |
|---|---|---|
| `touchline-local` | `touchline.local` | Persistent local backend; `--offline --no-captions` disables inference/transcription while private playback still needs AWS access |
| `touchline-demo` | `touchline.demo` | Finite demo with an explicit score/runtime mode |
| `touchline-captions` | `touchline.captions.tracks.cli` | Optional saved-track CLI; provider execution is explicit |
| `touchline-probe` | `touchline.tools.validation.replay_probe` | Bounded inference replay probe |
| `touchline-evaluate-director` | `touchline.tools.evaluation.director` | Offline validation replay with explicit research/event inputs |

For example, inspect the local launcher without starting it:

```powershell
& backend/.venv/Scripts/touchline-local.exe --help
```

The full [inference tool mapping](../docs/inference/README.md#operational-tools)
includes the other `touchline.tools.*` modules. Cloud operations need explicit
`--allow-cloud` and the applicable authorized credentials, artifacts and budget.
An estimated local ledger is not a provider billing cap. Routine tests, builds,
fixture sync and wheel verification do not invoke these paid operations.

Commands are grouped into `touchline.tools.cloud`, `.validation`, `.evaluation`
and `.legacy`. Shared receipt storage and separate budget implementations live
under `.support`; commands do not borrow those helpers from another command.
Legacy directory names in ignored artifacts, such as `overhaul` and
`winning-model`, are retained so existing credentials, receipts and budget history
continue to resolve. The [inference tool table](../docs/inference/README.md#operational-tools)
lists current modules and their explicit inputs.

The public `python -m touchline.captions` wrapper remains available. The live
caption subprocess instead runs `touchline.captions.live.worker`; it is started
by the backend adapter in its separately installed environment.
