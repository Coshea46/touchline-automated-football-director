> **Archived evidence.** This document records the original implementation and validation state before the repository refactor. Its commands, paths, branch names, resource observations and test counts are historical, not current setup instructions or a fresh cloud status check. Use [the current inference guide](../inference/README.md) for the installed package and tools.

# MatchPulse Modal deployment and validation — 7 November 2026

## A. Executive summary

Deployment succeeded, and a real L4-configured GPU worker started and completed
the extraction call before a diagnostic assertion failed. **No feature tensor
was successfully returned to the validation client. Numerical equivalence and
the public `features_mock` path remain unverified.**

The corrected request timed out after a 300-second result wait. Modal explicitly
reported waiting for `GPU_L4` worker capacity. Cloud execution stopped at a
conservative $2.8251 estimate. Only `matchpulse-inference` was stopped to prevent
late queued execution. Its final lifecycle is `APP_STATE_STOPPED`.

## B. Files changed

All authored changes are inside `backend/ml_inference/`:

| File | Purpose |
|---|---|
| `.gitignore` | Exclude private artifacts and isolated client environments |
| `config.py` | Default to one GPU container, one in-flight request, five-second idle scale-down |
| `extraction.py` | Expose cache reset and existing extractor diagnostics |
| `modal_app.py` | Bound resources; private numerical diagnostics; inspect the active CUDA prediction backend |
| `modal_client.py` | Record encoding, submission, wait and payload metrics |
| `cloud_validation.py` | Budgeted deployment, PTS sampling, equivalence gates, public-method cases and cleanup inspection |
| `tests/test_modal_client.py` | Verify the one-container default |
| `tests/test_worker_definition.py` | Resource/runtime regression checks, including active-backend CUDA evidence |
| `tests/test_cloud_validation.py` | Budget, deployment module-name and interval-centre sampling checks |
| `README.md` | Correct deployment instructions, defaults and actual validation status |
| `CLOUD_VALIDATION.md` | This report |

The ignored `.venv-deploy/` contains the isolated Python 3.12 client environment
with Modal 1.6.3, NumPy 2.1.3 and PyAV 17.1.0; no research environment was changed.
Private ignored `artifacts/cloud-validation/` contains:

- `deployment.log`, `deployment-002.log`, `deployment-003.log`,
  `deployment-004.log`, `deployment.json`: build/deployment outcomes.
- `budget.json`: retained build and call reservations, including failures.
- `sources.json`, `transport.json`: sampling metadata, hashes and compression measurements.
- `worker-startup.log`, `worker-corrected.log`, `worker-final.log`: startup,
  diagnostic and capacity messages.
- `initial-stats.json`, `idle-stats.json`, `provider-stats.json`,
  `startup-failed-provider-stats.json`, `provider-latest-stats.json`,
  `provider-final-stats.json`: bounded app-specific usage observations.
- `call-graph.json`, `final-call-graph.json`: request scheduling observations.
- `failure.json`, `startup-failure.json`, `device-check-failure.json`:
  sanitized failure metadata.
- `cleanup.json`, `app-lifecycle.json`, `result.json`, `isolation-check.json`:
  final outcome and cleanup/isolation evidence.

No raw frames, copied videos, credentials or signed blob URLs were written to
these artifacts. Shared application files, root `.gitignore` and frontend hashes
match the starting snapshot. Three research orchestration files changed during
the concurrent research session: `scripts/modal_app.py`, `scripts/run_modal.py`
and `config/modal.json`. Our commands did not write to the research repository;
it would be incorrect to claim the entire concurrently edited repository stayed
unchanged. Research runtime source and the protected application files matched.

## C. Modal deployment

- App: `matchpulse-inference`; environment: `main`.
- Registered classes: `FeatureWorker.*` (`extract`, `diagnose`) and
  `ClassifierWorker.*` (`ready`, `predict`).
- GPU: L4; FP16; CPU minimum 1 / maximum 4 cores; RAM 2 / 8 GiB.
- Feature cap: one container, serial inputs, zero warm/buffer minimum;
  five-second requested idle scale-down; 180-second execution and 300-second startup limits.
- Classifier: CPU-only, two cores, 4 GiB, maximum one, minimum zero; never invoked.
- Independent storage: `matchpulse-inference-models`; no classifier uploaded.
- Image: Python 3.13, Torch 2.11.0 / torchvision 0.26.0 from CUDA 13.0 wheels;
  pinned worker dependencies installed successfully, followed by the unchanged
  research wheel with `--no-deps`. YOLO26s weights passed their pinned SHA256 check.
- Runtime wheel SHA256:
  `d2eec093101eb85106ec2cff8329c61f3c386546244373a8f8ddebf674d62ef8`.
- YOLO weights SHA256:
  `646f8bc3fe0a656803d95c294f7852321748cb29d13466a1af8862e2db384a1b`.

Three issues were encountered: image `.env()` ordering after a source mount;
file-path deployment registering an unimportable `modal_app` module; and a CUDA
diagnostic inspecting the retained CPU source model instead of Ultralytics'
deep-copied prediction backend. The first two were fixed and the worker then
started. The diagnostic correction is deployed and locally regression-tested,
but its remote execution remains blocked by L4 capacity.

Deploy using `python -m modal deploy -m backend.ml_inference.modal_app` from the
project root. Final deployment controller wall time was 8.95 seconds. The app
was subsequently stopped; the independent Volume and cached image remain.
Private SDK input blobs may remain under Modal's retention; no footage was
written to the model Volume and no public endpoint was created.
No functions or resources belonging to `matchpulse-private` were invoked or changed.

## D. Real GPU extraction

Two existing local football windows were prepared: `football-five-seconds.mp4`
at 0–5 seconds and `2e57b9_0.mp4` at 5–10 seconds. These are two windows of the
same DFL/Roboflow sample, not two distinct matches. No dataset was downloaded.

Both inputs: `(75,1080,1920,3)`, RGB `uint8`, five seconds at 15 FPS. Sampling
chooses the latest source PTS at or before `start+(i+0.5)/15`, with coverage
checks and original pixels. Frame arrays remained in memory; input/source hashes
and chosen timestamps were recorded. Only the first window reached cloud
attempts; the second was prepared and compressed locally.

One GPU worker passed CUDA availability initialization and ran the unchanged
research extraction adapter. Its exception occurred after `extract()` returned
and its shape/dtype/finiteness/missing-audio validator passed. This is evidence
of remote extraction completion, not a successfully returned smoke-test result.
The old diagnostic then inspected the CPU source model and raised. Explicit
active-backend CUDA/FP16 evidence, returned input hash, returned match ID and
A–B–A request-state checks have not been obtained.

Expected output remains `(75,215)` `float32`, 64,500 bytes, with zero audio
columns `210:215`; no client-observed tensor is claimed.

## E. Numerical equivalence

Not established. The diagnostic uses a fresh research
`matchpulse.features.MatchFeatureExtractor` with identical configuration and
the same stateless YOLO detector on the same GPU/precision, with fresh tracking
and raw-detection caches. Fixed tolerances remain `atol=2e-5`, `rtol=1e-5`.

Maximum absolute difference, outside-tolerance count, slot/team agreement,
audio agreement and camera-cut agreement are unavailable because no comparison
completed. The gate remained closed; broader benchmarking did not start.

## F. End-to-end inference test

The exact public signature remains
`select_best_match(current_match_id, match_frames) -> str`.
The controller implements one-match, two-window, challenger-wins and
staying-bonus cases using real Modal extraction plus deterministic mock scores.
**Zero of these cloud cases ran**, because equivalence did not pass.
Local fixture tests cover the behavior; they do not substitute for cloud proof.

Real GRU inference was not tested. A trusted portable classifier bundle must
be loaded into the independent model Volume and validated before `real` mode.

## G. Performance

| Measurement | Window A | Window B |
|---|---:|---:|
| Raw bytes | 466,560,000 | 466,560,000 |
| Lossless compressed bytes | 107,473,020 | 217,352,131 |
| Compressed / raw | 23.04% | 46.59% |
| Compression ratio | 4.34× | 2.15× |
| Latest local compression | 5.920 s | 13.171 s |

Provider metrics for the **single failed request that executed**: startup
9.576 seconds, execution 15.166 seconds and provider end-to-end latency
27.769 seconds. These are single failed-request observations, not successful
public-method latency or tail estimates. The corrected call waited 300 seconds
for its result without a container task being assigned. Modal explicitly logged
waiting for L4 capacity; allocation latency is therefore censored by the timeout.

Warm latency, model-loading breakdown, upload-only time, decompression time,
YOLO time, tracking/assembly time, successful total extraction time and public
method latency were not measured successfully. No p95/p99 or seven-stream
capacity claim is justified. No valid comparison to the earlier Kaggle offline
75 FPS detector / 16.6 FPS full-pipeline throughput can be made.

Local compression and large WAN payloads are clear costs; transport dominance
over successful inference is not yet established. Next investigate lossless
Zstandard/LZ4 and parallel frame compression, and locate the client near the
GPU worker. Validate exact pixel round-trips and retain the public signature,
75 frames and interval-centre timing. No lossy codec, resize, skipping or complex
caching was introduced.

## H. Costs

Final conservative controller estimate/reservations: **$2.8250928**, below $3.
It retains four $0.60 build/deployment allowances and three $0.1416976 GPU-call
allowances, including failed or unconfirmed work. This is deliberately more
conservative than charging only the one completed execution.

Rates used: L4 $0.000222/s, CPU $0.0000131/core/s and RAM
$0.00000222/GiB/s ([Modal pricing](https://modal.com/pricing)). At the requested
maximum four cores / eight GiB, the modeled worker rate is $0.00029216/s.

Observed on the post-redeployment app: one started container and one failed
executed input; the earlier app also recorded two startup errors. Provider-billed
GPU seconds, actual dollar charges and an invoice were not available. The
15.166 seconds of execution is not total billed GPU lifetime. No confirmed
unexpected charge can be reported; absence of an invoice does not prove zero
charges. No further cloud work was launched after the final timeout. The final
app lifecycle is stopped, and no worker remains continuously warm.

## I. Tests and isolation

- Final local suite: **113 passed**, zero failed, zero skipped, including the
  original 105 tests. These local tests do not launch paid cloud work.
- Ruff: all checks passed using `--no-cache --isolated`.
- Python 3.10 syntax validation passed for all 22 package/test Python files.
- Branch remains `feature/modal-ml-inference`; HEAD
  `c43fdf32c5110c98d59bc53943b3403d36240e8e`.
- No branch switch, commit, push, merge, reset or other Git-history mutation was issued.
- Protected application files and research runtime source matched the starting
  hash snapshot; the three concurrent research changes are disclosed above.
- Cloud artifacts stayed private; there is no public HTTP footage endpoint.
- Final app state was independently read as `APP_STATE_STOPPED` after cleanup.

Unverified: corrected real CUDA diagnostic, returned feature contract,
equivalence, cross-request state isolation, public cloud cases, trained classifier
predictions, successful latency breakdown and multi-match capacity.

## J. Remaining work

1. Resolve L4 capacity and authorize a new bounded validation budget; redeploy
   the corrected module and pass returned-tensor, equivalence and A–B–A gates.
2. Run the four public `features_mock` cases and capture stage/RPC timings.
3. Have the backend teammate supply compliant RGB/PTS windows and the real
   current match ID, replace the dummy switch, and preserve ordered results.
   The exact integration caveats and example are in `README.md`.
4. Upload a versioned trusted portable GRU bundle to inference-only storage;
   check saved preprocessing, missing-audio behavior and real predictions.
5. Obtain seven distinct authorized match samples, improve measured transport
   costs without changing pixels/timing, and benchmark before claiming an
   operational seven-match MVP.
