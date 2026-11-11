> **Archived evidence.** This document records the original implementation and validation state before the repository refactor. Its commands, paths, branch names, resource observations and test counts are historical, not current setup instructions or a fresh cloud status check. Use [the current inference guide](../inference/README.md) for the installed package and tools.

# Touchline streaming inference

The production path is `backend.main` → `ReplayWebSocketClient` → authenticated
`ReplayWorker.web` ASGI sessions → compact scores → backend director → browser
WebSocket. The persistent private worker socket carries controls and scores
without per-update Modal Dict RPCs. `ReplayClient` remains an explicit diagnostic
transport selected with `MATCHPULSE_REPLAY_TRANSPORT=dict`.
The former complete-window array API remains available for compatibility tests;
it is no longer called by the application. See the root [README](../../README.md)
for launch configuration.

The product is named Touchline; existing `matchpulse` resource/package names,
`MATCHPULSE_*` settings and authentication headers remain unchanged for compatibility.

## Runtime and timing contract

* Independent app: `matchpulse-inference`; model Volume:
  `matchpulse-inference-models`. The retained diagnostic Dict is
  `matchpulse-inference-replay`; production sockets do not use it.
* One logical source reader/cache per registered match, at most seven sources.
  One physical T4 per two sources by default; seven use 2+2+2+1 across four T4s.
  Explicit `MATCHPULSE_GPU_WORKERS` can choose one through four. Capacity must be
  established by benchmarks, not inferred from these limits.
* Fixed GPU pool, no session/model/shard constructor parameters. At most four
  containers, zero minimum/buffer containers, five-second idle scale-down,
  no automatic Modal retries. Each input has a finite lifetime and viewer lease.
* Each container has 4 reserved / 8 maximum CPU cores and 4 / 8 GiB RAM.
  Shared YOLO26s FP16 CUDA batches have at most 16 frames. CPU observations
  overlap GPU detection, with at most one pending visual job per source.
* Each source directly range-reads its original HTTPS encoded media with two
  persistent PyAV readers. The application backend handles no decoded media.
  Use public demo sources or expiring backend-generated authorized URLs.
* Sampling uses the frame displayed at `offset+(index+.5)/15`, with actual PTS.
  The common replay clock advances only from the controlling player's actual
  `currentTime-offset`. A stalled/paused/absent viewer grants no future permit.
  Seek/discontinuity changes `clock_epoch` and discards the observation cache.
* Complete windows are `[0,5]`, `[2,7]`, `[4,9]`, etc. Each requires 75 sampled
  RGB frames and aligned 16 kHz mono/stereo PCM. New windows reuse 45 observations
  and infer only the 30 new frames. No future observations enter a window.
* Only frame observations are cached (75 rows, no RGB in that cache). Fresh
  completed-window tracking, team assignment, slots and interpolation use the
  unmodified vendored research builder. Schema remains `matchpulse-215-v1`,
  float32 `(75,215)`, with audio in channels 210:215.
* Reader queues hold at most 32 RGB samples; source native-audio history is
  bounded to 12 seconds / 32 MiB. No unbounded media queue or raw frame upload.
* The selected model uses the released `sampleclock-v3` native clock, preserving
  genuine gaps as missing samples and repairing only channels 210:215. Native
  PCM is cropped before FIR resampling; no future audio enters a window. The
  frozen decoder and core-five feature parity are independently checked. The
  explicit legacy model retains its original `legacy-gru` audio contract and
  2 ms cumulative tolerance. Missing audio is never fabricated silence.
* Each shard has one bounded daemon network thread with a persistent TLS socket,
  a latest control value and a latest compact snapshot. Server publication is
  coalesced, with one pending snapshot. Controls preserve the viewer's position,
  epoch and original expiry; the transport never extrapolates or renews a lease.
  Source open/read failures have bounded, isolated recovery. The separate
  `LatestStateChannel` Dict IO implementation is diagnostic, not the default path.

## Bridge and director

`ReplayWebSocketClient.start(spec)` validates a fresh viewer lease and starts one
to four owned socket sessions on the same fixed `ReplayWorker` GPU pool. Seven
sources partition into 2+2+2+1; each socket receives only its own source
updates. A cold handshake may take up to 90 seconds; the freshest local control
is sent after it completes. Control updates and score reads use bounded local
caches, with no Modal SDK operation on the update path.

The client reuses `ReplayClient` partitioning and strict session/model/epoch
merging, with a monotonic sequence that remains unchanged on duplicate polls.
All workers must be ready before playback leaves the startup buffer. The backend's
validated `startup_waiting` control keeps a fast shard out of the normal 30-second
pause timer while the group is preparing, bounded by the existing 120-second
startup/session deadline and fresh viewer lease. Clearing that barrier starts the
ordinary idle countdown; older controls without the field retain their old guard.
EOF removes
ended sources and preserves remaining healthy sources. Stop sends inactive
control and waits for a stopped acknowledgement emitted after the runner's
`finally` cleanup, or a normal server close. A lost connection or missing
acknowledgement is reported as unconfirmed; it relies on disconnect/lease expiry
and a separate resource check, not an assumed GPU cancellation.

The diagnostic `ReplayClient` instead uses `control:<session_id>` and
`scores:<session_id>` (or `:0` through `:3` score suffixes). Its finite SDK deadlines,
owned-call cancellation and cleanup remain isolated from production socket IO.

Scores include match ID, model version, clock epoch, sequence, completed interval,
source endpoint, availability/production timestamps, score kind, probability and
stage timings. Health includes source progress and bounded queue state. Compact
records contain no RGB, PCM or feature tensors.

The backend director compares the same completed endpoint across healthy
matches, adds a noncumulative configured current-match bonus, and rejects stale, future,
unknown, old-session/model/epoch and out-of-order scores. It keeps at most two
records per source, applies deterministic ties, minimum switch spacing and manual
hold, then emits a validated WebSocket recommendation. Current selection is
committed after a player ACK or actual playback feedback. Network handlers do no
SDK work. State delivery retains only the latest message per kind.

Balanced defaults to a 12-second automatic cooldown, 0.20 staying bonus and two
consecutive fresh challenger wins; Relaxed is 20 / 0.25 / 3 and All Action is
4 / 0.05 / 1. Inference remains on the unchanged two-second grid. Cooldown starts
on actual automatic-selection acknowledgement. Manual choices hold for 30 seconds;
the conservative guard also follows factual playback source changes, without
classifying expired proposals as valid model selections. Manual hold takes priority
over a fallback when the selected source's ML monitoring is unavailable.
Auto off still monitors but does not select a different source. Mode changes use
the application WebSocket and never alter or restart a Modal worker. The optional
strong-margin confirmation bypass is disabled by default. See the root README
for the seven-source `python -m backend.demo` browser launch and settings contract.

## Installed model and handoff

The default is `temporal-shots3_10-d63fff64480d/classifier.pt`, checkpoint SHA256
`d63fff64480d00835c14bc7c726e062b00278126d517e1e9eceb1abe0aafbd1e`.
`load_replay_scorer` delegates to the exact released
`matchpulse.research.predictor.load_selected_predictor`, using `shots3_10`.
Its raw `(75,215)` float32 observations become 34 value channels plus 34 validity
channels with saved train-fitted normalization. The stateless one-layer GRU has
68 inputs, hidden size 16, three heads and 4,179 parameters. It stays loaded on
CPU beside GPU extraction; scoring requires no additional network hop.

Canonical cloud readiness, release score parity (absolute error `4.66e-08`), six
direct-source reference windows (zero feature difference) and a 30-second real
seven-source replay passed. That replay delivered 13/13 full cohorts at endpoints
5,7,...,29, with 1.734/1.859/2.109s minimum/median/maximum delivery latency. These
are capacity and correctness results, not predictive-quality measurements.

Auto is requested On by default but becomes effective only after monitoring
readiness. The unchanged production modes differ from the research selection
policy: Balanced rarely switches on the new raw scores. See the root README and
`evaluate_director.py` for the validation replay, timing assumptions and limits.

The explicit rollback portable full-audio GRU remains installed as
`gru-stage1-02de8309/classifier.pt` in the inference-only Volume. Its SHA256 is
`02de83095e5eaaf2f22978e89eec8515633ecd680ba0e9834cfb46d8cbea9319`.
Its CPU readiness was reverified under the deployed runtime. Historical legacy
seven-source GPU real-model replay also passed.
The 60-second run used four T4s, delivered all 28 recommendations with all seven
source scores, and met the three-second gate. The prior
Dict run computed all 20 per-match windows but delivered 8/10 recommendations;
that result does not establish the new socket transport's latency or capacity.

Predictive quality is weak: validation average precision is **0.051729** against
prevalence **0.051133**, with ROC AUC **0.476397** and no held-out test access.
Scores are uncalibrated; these results do not establish reliable event anticipation
or multi-match director quality. `features_mock` still runs real video/audio
extraction with explicitly synthetic scores and establishes infrastructure only.
`real` never silently falls back to a mock.

To install the trusted export in another authorized inference workspace, use
`classifier.pt`, not training-only `best.pt`, with its exact SHA256 and immutable
version directory:

```powershell
python -m backend.ml_inference.install_checkpoint --allow-cloud --trusted-checkpoint ../cheap-classifier-model-research/artifacts/stage1-mvp/classifier.pt --version gru-stage1-02de8309 --sha256 02de83095e5eaaf2f22978e89eec8515633ecd680ba0e9834cfb46d8cbea9319
$env:MATCHPULSE_MONITOR = 'true'
$env:MATCHPULSE_ML_MODE = 'real'
$env:MATCHPULSE_ML_CHECKPOINT = 'gru-stage1-02de8309/classifier.pt'
$env:MATCHPULSE_MODEL_VERSION = 'gru-stage1-02de8309/classifier.pt'
$env:MATCHPULSE_MOCK_SCORES_JSON = ''
```

The upload targets only the inference Volume, refuses overwrite, limits the
bundle to 64 MiB, verifies the local and remote SHA256, and runs a finite CPU
readiness check. No training workload or research Volume is accessed. Use a new
version path for each checkpoint: warm workers cache at most two immutable paths.

Readiness loads through `matchpulse.load_interest_classifier`, checks the exact
75×215/15 FPS/five-second schema, all 215 channels, GRU input 215 / hidden 64 /
one unidirectional layer, eval mode, saved train-fitted normalization and finite
statistics, validation-selected threshold, and dataset audit provenance. The
research loader owns strict state loading, normalization and sigmoid inference.
The GRU runs on CPU inside the persistent GPU worker; tensors stay in that
container. GPU and CPU paths resolve the model mount before enforcing containment;
checkpoint failures expose only bounded phases and static error codes.

## Deploy and validate

The current full local suite passed **779 Python tests, with one optional
Torch/runtime test skipped**. Separate installed-runtime checks passed 18 tests;
the canonical cloud readiness gate also passed. Ordinary tests use NumPy, pytest
and the backend WebSocket dependency, with
no credentials, footage, CUDA or downloads:

```powershell
python -B -m pytest backend/ml_inference/tests backend/tests -q
```

Use a Python 3.12+ Modal deployment environment, authenticated to your workspace.
The owned runtime wheel is pinned in `runtime/provenance.json`; detector weights
must match the expected checksum. The sibling research source is read-only.

```powershell
python -m pip install modal==1.6.3
$env:MATCHPULSE_ML_GPU = 'T4'
$env:MATCHPULSE_ML_WEIGHTS = '../cheap-classifier-model-research/yolo26s.pt'
python -B -m backend.ml_inference.overhaul_cloud setup-ws --allow-cloud
python -B -m backend.ml_inference.winning_model deploy --allow-cloud
```

For the current helper, use the verified application-local YOLO copy at
`artifacts/model-release/yolo26s.pt`; the helper overrides the old sibling path
before invoking Modal. Existing WebSocket credentials are reused. The current
deployment/install/equivalence/probe operations use a separate $3 task ledger,
preserving historical ledgers. See the root README for immutable selected-model
installation and explicit legacy readiness commands. The preserved original
runtime wheel is `runtime/matchpulse-0.1.0-py3-none-any.whl`; the selected wheel
adds the new loader while retaining the validated legacy model API.

Setup creates an inference-only proxy credential and the app Secret
`matchpulse-inference-replay-auth`. The endpoint requires proxy authentication
before GPU cold start and app authentication on the same private credential.
Deployment records the URL and token in ignored
`artifacts/overhaul/ws-credentials.json`. Load them only into the CPU backend;
never print them or put them in frontend build variables:

```powershell
$wsCredentials = Get-Content -LiteralPath 'backend/ml_inference/artifacts/overhaul/ws-credentials.json' -Raw | ConvertFrom-Json
$env:MATCHPULSE_REPLAY_TRANSPORT = 'websocket'
$env:MATCHPULSE_MODAL_WS_URL = $wsCredentials.url
$env:MATCHPULSE_MODAL_WS_TOKEN = $wsCredentials.token
```

The client uses `Authorization: Bearer` and `X-MatchPulse-Replay-Token` over the
private `wss://...modal.run/session` endpoint. The browser connects only to the
local application backend; it receives neither credential nor worker URL.

The finite task controller loads the private credential file itself and reserves
an estimated budget before paid work. Probe mode is explicit (`features_mock` is
the CLI default, so specify `--mode real` for the installed GRU):

```powershell
python -m backend.ml_inference.overhaul_cloud equivalence --allow-cloud
python -m backend.ml_inference.overhaul_cloud probe --matches 2 --seconds 24 --mode real --allow-cloud
python -m backend.ml_inference.overhaul_cloud probe --matches 7 --seconds 60 --mode real --allow-cloud
python -m backend.ml_inference.overhaul_cloud idle --allow-cloud
```

Overlap equivalence gates wider tests. It compares three successive windows on
two distinct S3 sources with fresh detector/research assemblies, exact selected
pixels, PTS and native-cropped audio. Tolerances are fixed at `atol=2e-5`,
`rtol=1e-5`. Local source sampling is checked separately against research decoding.
The completed proof covered all six overlapping windows: maximum absolute
difference **0**, with **0** out-of-tolerance elements. The feature/audio/schema
contract is unchanged by the transport replacement.

The paced probe runs the actual Modal client, backend director and WebSocket with
a headless viewer clock. It reports every expected two-second endpoint, missing
and censored endpoints, 3-second misses, bounded queues, stage timings, resource
samples and delivery latency. Its 3.25-second drain freezes playback and keeps
heartbeats active. It does not test HTML video playback. `passed` requires complete
cadence, every registered source score in every recommendation, and no run errors;
healthy-subset recommendations remain valid application messages but fail capacity.
`three_second_target_met` is reported separately and must also be true
for the latency target. A finite browser helper is available as
`python -m backend.ml_inference.overhaul_browser --allow-cloud --seconds 120 --mode real`
with a frontend built for `ws://127.0.0.1:8020`, port 3020.

Measured transport results, including failed historical attempts:

| Sources / GPUs | Model / transport | Validation status |
|---|---|---|
| 2 / 1 T4 | Real GRU / Dict diagnostic | 20/20 per-match windows computed; 8/10 recommendations delivered |
| 2 / 1 T4 | Real GRU / private WebSocket | 10/10 recommendations; 1.453–1.765 s, median 1.5465 s, no 3 s misses |
| 7 / 2 T4 | Real GRU / private WebSocket | 0/28 recommendations; 142/196 per-match windows; capacity failed |
| 7 / 4 T4, before decoder priming | Real GRU / private WebSocket | 16/28 recommendations; early reader stalls; full run failed |
| **7 / 4 T4, final** | **Real GRU / private WebSocket** | **28/28 full seven-source cohorts; 1.578–2.157 s, median 1.899 s; no 3 s misses** |

Final receipt: ignored `artifacts/overhaul/probe-7-11.json`, real checkpoint
`gru-stage1-02de8309`. It recorded 196 completed per-match windows, 6,300 newly
sampled frames at the paced 105 aggregate FPS requirement, zero decoder failures
and zero worker deadline misses. Startup buffering was 19.547 s. All source
watermarks reached 900, with zero final queues/backlog after the drain and a maximum
actual queue of 21 samples. Owned WebSocket stops were confirmed after cleanup;
the final resource check observed zero running inputs/backlog in every owned cloud
pool. The preferred sub-second target remains unmet; this was a one-minute replay,
not a full-match or soak test.

The four-GPU default follows the measured two-source worker limit. The worker
carrying three sources completed all 84 windows but missed 11 processing deadlines;
four sources developed backlog and six audio-history failures. GPU utilization
was low; detector wall time includes host preprocessing, and the research tracker
serializes window tracking to protect global IDs. The image requested one numerical
thread, but measured OMP/OpenBLAS/MKL limits were 4/4/4 and NumExpr 1 in both final
runtime and equivalence receipts. Their independent speedup has not been measured.
Report complete cadence, endpoint-to-recommendation latency and post-compute
delivery separately. A headless clock probe does not prove HTML player switching;
the browser helper must validate that path and retain the explicit score mode.
The original infrastructure browser verification was blocked before a viewer
joined. Subsequent product work observed the redesigned light UI and playback,
and a seven-source offline session recorded real browser/manual-selection reports.
Full refreshed real-model browser switching remains unverified; current product
test results and exact browser limits are in the root README. Docker CLI was
unavailable; container image builds were not tested here.

Raw logs/results and conservative task budget reservations are ignored under
`artifacts/overhaul/`. Reservations include cold-start and idle allowance and
multiply by GPU worker count. They are estimates, not provider billing or a hard
spending cap. Prior stateless validation history is retained separately in
[AUDIO_VIDEO_VALIDATION.md](AUDIO_VIDEO_VALIDATION.md) and
[CLOUD_VALIDATION.md](CLOUD_VALIDATION.md).

## Legacy interface

`select_best_match(current_match_id, match_frames, match_audio)` still accepts
complete 75-frame RGB / five-second 16 kHz PCM windows for compatibility tests.
It delegates to the previous stateless worker and is unsuitable for live replay.
The production entry point never calls it. There is no change to historical
research/data/model artifacts.
