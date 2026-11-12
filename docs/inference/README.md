# Touchline streaming inference

The installed `touchline` package preserves the existing streaming path:
`touchline.main` → `ReplayWebSocketClient` → authenticated `ReplayWorker.web`
sessions → compact scores → CPU director → browser WebSocket. The persistent
worker connection carries controls and scores; ordinary updates do not use Modal
Dict RPCs. `MATCHPULSE_REPLAY_TRANSPORT=dict` retains the diagnostic `ReplayClient`.
The complete-window array API remains a compatibility interface.

See [backend setup](../../backend/README.md) and the [root README](../../README.md)
for installation and local launch. Source lives in
`backend/src/touchline/ml_inference/`; operational commands live separately in
`backend/src/touchline/tools/`.

## Source groups

| Inference package | Responsibility |
|---|---|
| `features/` | Encoded-media readers, audio alignment, incremental extraction and feature validation |
| `models/` | CPU classifier adapters and pinned release identity/support |
| `streaming/` | Persistent clients, session/control contract, ASGI endpoint, worker runner, state and diagnostics |
| `deployment/` | Modal application, verified image inputs and worker requirements |
| `window_api/` | Retained API for caller-supplied complete video/audio windows |

`config.py` and the public package exports remain at the inference root. Existing
imports such as `from touchline.ml_inference import InferenceConfig,
InferenceService, select_best_match` remain supported. Internal paths now use the
groups above; the persistent WebSocket client is
`touchline.ml_inference.streaming.ws_client`, while deployment uses
`touchline.ml_inference.deployment.modal_app`.

The streaming package concerns worker execution and transport. Application
replay ownership, playback permits and director policy remain in
`touchline.replay`; these are separate responsibilities within one application.

## Runtime contract

- Seven distinct source readers/caches are distributed over four GPU workers
  by default (2+2+2+1), with T4/L4 capacity fallback in the deployment.
  `MATCHPULSE_GPU_WORKERS` can select one through four workers. A public browser
  run used three T4 workers and one L4 worker. The pool now keeps eight replay
  containers warm, prefers L4 with T4 fallback, and preloads both models; overflow
  containers have a 300-second idle window. GPU choice preserves the frozen
  model and contracts below.
- Workers read encoded source media directly. Observations are sampled at 15 FPS;
  complete five-second windows contain 75 rows and advance every two seconds.
  Overlapping windows reuse observations rather than repeating YOLO detection.
- The raw contract remains float32 `(75,215)`, including audio channels 210:215.
  The selected release uses the `sampleclock-v3` audio clock and preserves missing
  samples. The explicit legacy model keeps its original audio contract.
- The selected CPU GRU uses the released `shots3_10` head, 68 transformed input
  channels and 4,179 parameters. It runs beside extraction inside the existing
  worker. Scores are relative rankings, not calibrated goal probabilities.
- Session/model/clock-epoch identity, fresh viewer leases, sequence numbers and
  completed timestamps protect playback alignment. Queues are bounded; owned
  sessions stop on cleanup or lease expiry. Playback permits do not extrapolate
  into unviewed future footage.
- The backend compares aligned, fresh score cohorts and enforces cooldowns,
  staying bonuses, distinct challenger confirmations and the 30-second manual
  hold. Player acknowledgement commits a switch; inference stays on its
  two-second grid independently of switching frequency.

The configured selected checkpoint is
`temporal-shots3_10-d63fff64480d/classifier.pt` with SHA256
`d63fff64480d00835c14bc7c726e062b00278126d517e1e9eceb1abe0aafbd1e`.
The explicit rollback version is `gru-stage1-02de8309/classifier.pt`. Release
identities and checksums are defined in `touchline.ml_inference.models.release`.

## Packaging and private configuration

The backend wheel includes the audited runtime wheels, decoder support,
release manifests and smoke fixtures under `touchline/ml_inference/runtime/`.
The separate Modal image installs `deployment/requirements-worker.txt`, uses the
pinned research runtime and checks local YOLO weights through
`touchline.ml_inference.deployment.packaging.runtime_artifacts`. Running the application
does not import a sibling research checkout. Trusted YOLO/classifier weights,
private credentials and historical result receipts must be supplied separately.
The image mounts the complete inference package, including its subpackages and
frozen runtime assets. Git line-ending rules preserve the decoder and smoke
fixture bytes required by their checksums.

Compatibility names remain `matchpulse-inference`,
`matchpulse-inference-models`, `MATCHPULSE_*` and the existing authentication
headers. The retained diagnostic Dict is `matchpulse-inference-replay`.

Writable inference output now lives under `backend/artifacts/inference/`;
model-release inputs live under `backend/artifacts/model-release/`. The optional
private socket credentials file is
`backend/artifacts/inference/overhaul/ws-credentials.json`, or configure
`MATCHPULSE_MODAL_WS_URL` and `MATCHPULSE_MODAL_WS_TOKEN` server-side. The browser
connects to the CPU backend and never receives these worker credentials.
`MATCHPULSE_PROJECT_ROOT` selects the configuration/artifact root for installed
or hosted environments; see [architecture](../architecture.md).

## Operational tools

Use the installed interpreter: `python -m touchline.tools.<group>.<module>` after
activating the backend environment, or use its full executable path. Current
cloud/model operations, validation, evaluation and retained historical workflows
have separate groups. Shared storage and independent ledger policies live under
`touchline.tools.support`, and subprocess environment profiles live in
`touchline.config.cloud`.

| Module under `touchline.tools` | Purpose and explicit inputs |
|---|---|
| `cloud.model_release` | Selected-model `deploy`, `install`, `equivalence` and `probe` operations require `--allow-cloud`; `idle` reads existing worker statistics; explicit legacy verification remains available |
| `cloud.checkpoints` | Immutable inference-volume upload/readiness check; requires `--allow-cloud`, `--trusted-checkpoint`, `--version` and `--sha256`; `--verify-only` checks an existing version |
| `validation.smoke` | Bounded remote extraction smoke check; requires `--allow-cloud` |
| `validation.replay_probe` | Bounded headless replay probe; requires `--allow-cloud` and `MATCHPULSE_MONITOR=true` |
| `evaluation.director` | Local replay evaluation; requires both `--research-root` and `--events` |
| `legacy.window_validation` | Historical `deploy` or `validate` workflow; requires `--allow-cloud`; `validate` also requires `--research-root` |
| `legacy.t4_audio_video` | Historical local validation-media preparation; requires `--research-root`; paid validation is dispatched by `legacy.window_validation` |
| `legacy.streaming` | Historical finite `deploy`, `equivalence`, `probe`, `idle`, `install`, `diagnose` and `setup-ws` operations; requires `--allow-cloud`; `install` also requires `--trusted-checkpoint` |
| `legacy.browser` | Historical finite browser-path helper; requires `--allow-cloud`, with score mode and bounded duration |

Installed aliases include `touchline-probe` and
`touchline-evaluate-director`. Inspect a command's `--help` before an explicitly
authorized operation. No command here is part of normal installation or testing.
The `features_mock` mode still performs real media extraction; it uses synthetic
scores and is not a free/offline mode. `real` does not silently fall back to it.
Cloud ledgers are application estimates rather than provider-enforced billing caps.

Renaming commands does not combine or renew their independent allowances:

| Workflow | Existing estimated ledger | Preserved directory under `backend/artifacts/inference/` |
|---|---:|---|
| Selected-model release | $3, locked; uncertain cleanup retains its reservation | `winning-model/` |
| Historical streaming and browser validation | $10 | `overhaul/` |
| Historical complete-window/T4 validation | $5 | `t4-av-validation/` |

The prior `cloud-validation/` and `t4-av-validation/` histories retain their
existing roles in those ledgers. The `overhaul/` credential path is also preserved.
Per-launch demo, inference-session and caption allowances remain separate.

Research-based tools no longer assume a sibling directory. Supply an authorized
research checkout through `--research-root`, and supply the exact validation event
Parquet file to `evaluation.director --events`. These paths are inputs, not permission
to modify research or training data. Optional media/model/evaluation dependencies
and authorized data must already be available for the chosen tool.

## Verification and evidence

Run provider-free tests from the repository root:

```powershell
& backend/.venv/Scripts/python.exe -B -m pytest -c backend/pyproject.toml backend/tests
```

The [package verifier](../../scripts/README.md) checks installed-wheel assets,
entry points and caption subprocess startup without cloud calls.

On 12 November 2026, a bounded T4 operator check produced complete real-model
cohorts across all seven sources at observation ends of 5 and 7 seconds, with
21.415-second startup and confirmed owned GPU/source cleanup. A later public
browser run used three T4 workers and one L4 worker, observed automatic source
switching and focused Scribe delivery, and confirmed cleanup. Its strict check
for repeated complete seven-source score cohorts failed; sustained scoring in
the joined public path remains unverified. Earlier T4 queueing exceeded the
90-second worker connection timeout. Hosted configuration now sets
`MATCHPULSE_MODAL_WS_CONNECT_TIMEOUT=110`; the generic default stays 90 seconds
and public Director and caption runs are independently capped at 1200 seconds.
Manual seven-source browser playback and focused Scribe delivery passed
separately. Current reservations are $2.60 per Director run, including the broad
EU placement premium, and $0.14 per caption
run; see [hosted admission and verification](../modal-backend.md). The older live
checks below used the former two-minute policy; the twenty-minute policy has
provider-free lifecycle and clock-expiry tests, without a new paid AI run.

Historical measurements are preserved separately:

- [Streaming inference handoff](../validation/STREAMING_INFERENCE_HANDOFF.md):
  selected-model parity, bounded seven-source results, legacy comparisons and
  the original operational instructions.
- [Audio/video T4 validation, 8 November 2026](../validation/AUDIO_VIDEO_VALIDATION.md).
- [Initial Modal validation, 7 November 2026](../validation/CLOUD_VALIDATION.md).

Those reports record their original paths, test counts, failures and resource
observations. They are archival evidence, not fresh deployment checks. The root
README retains the selected model's measured results and their limitations.
