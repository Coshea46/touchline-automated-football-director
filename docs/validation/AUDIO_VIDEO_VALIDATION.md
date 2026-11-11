> **Archived evidence.** This document records the original implementation and validation state before the repository refactor. Its commands, paths, branch names, resource observations and test counts are historical, not current setup instructions or a fresh cloud status check. Use [the current inference guide](../inference/README.md) for the installed package and tools.

# MatchPulse audio/video inference — T4 validation, 8 November 2026

## A. Summary

The required three-argument audio/video API is implemented and operational in
`features_mock` mode through actual Modal T4 extraction. Two distinct real
football windows passed numerical equivalence with **maximum absolute
difference 0.0**, zero elements outside tolerance, genuine non-silent stereo
audio and all 75 audio-availability flags set. Same-worker A–B–A output was exact.
All four public-method cases returned their expected match IDs.

This is not trained-model prediction validation. The portable full-audio GRU
is unavailable. Also, measured method latency is **35.73 seconds for one window
and 66.43–77.90 seconds for two**, which misses the 3–10-second anticipation
horizon. The current measured path is not ready for event anticipation.

The inference app remains deployed with **zero containers, inputs and backlog**.
Additional conservative spending estimate is **$0.715808**, below the $5 ceiling.

## B. Files changed and ownership

All authored source, tests and documentation are inside `backend/ml_inference/`.

| Files | Change |
|---|---|
| `api.py`, `__init__.py` | Required audio argument; explicitly named legacy video-only test API |
| `validation.py`, `service.py` | Audio validation, exact key correspondence, complete-audio feature checks and associated dispatch |
| `transport.py`, `modal_client.py` | Versioned lossless audio/video envelopes, identity checks and timing metrics |
| `extraction.py` | Delegate raw mono/stereo waveform to the existing research extractor |
| `classifiers.py` | Require complete audio features and a full 215-channel portable bundle |
| `config.py`, `modal_app.py` | T4 default, actual CUDA/FP16 verification, full-audio extraction and same-worker equivalence diagnostics |
| `cloud_validation.py`, `t4_validation.py` | Persistent $5 ledger, bounded deployment, real-data gates, public tests and cleanup |
| `sample_preparation.py`, `cloud_smoke.py` | Strict local broadcast decoding and explicit synthetic audio/video smoke support |
| `tests/test_audio_video.py`, `tests/test_sample_preparation.py` | New audio, association, transport, failure and decoder regression tests |
| `tests/test_classifiers.py`, `tests/test_cloud_validation.py`, `tests/test_contract.py`, `tests/test_modal_client.py`, `tests/test_pipeline.py`, `tests/test_worker_definition.py` | Adapt existing tests to the new production contract, explicit legacy path and T4 resources |
| `README.md`, `AUDIO_VIDEO_VALIDATION.md` | Current contract, deployment, checkpoint process, integration instructions and this report |

The old `CLOUD_VALIDATION.md`, runtime wheel/provenance, worker requirements and
package metadata were preserved. The ignored `.venv-deploy/` gained only its
own local research-wheel/decoder dependencies; no research environment changed.

Ignored private `artifacts/t4-av-validation/` contains `deployment-001.log`,
`deployment.json`, `budget.json`, `sources.json`, `diagnostics.json`,
`public-method.json`, `result.json`, `provider-initial-stats.json`,
`provider-final-stats.json`, `idle-stats.json`, `cleanup.json`, `worker.log`,
`performance-summary.json`, `isolation-check.json`, `final-checks.json` and
`app-final-state.json`. Private helper scripts in `artifacts/` inspect source
audio, capture only inference logs, summarize measurements and verify final
state. Temporary migration scripts were removed.

All **93 protected-file hashes match this task's starting snapshot**, including
shared application/frontend files and research source/configuration. No research
files were written, no research controllers/functions were invoked, and no
training/extraction jobs were stopped. Branch remains `feature/modal-ml-inference`,
HEAD `c43fdf32c5110c98d59bc53943b3403d36240e8e`. No Git-history mutation was issued.

## C. Updated public contract

```python
def select_best_match(
    current_match_id: str,
    match_frames: dict[str, np.ndarray],
    match_audio: dict[str, np.ndarray],
) -> str:
    ...
```

Executable local example from the repository root, with explicitly synthetic
scores and no GPU work:

```python
import os
import numpy as np
from backend.ml_inference import select_best_match

os.environ['MATCHPULSE_ML_MODE'] = 'mock'
frames = {key: np.zeros((75, 90, 160, 3), np.uint8)
          for key in ['match_01', 'match_02']}
audio = {key: np.zeros(80000, np.float32) for key in frames}
selected = select_best_match('match_01', frames, audio)
assert selected == 'match_01'
```

`features_mock` runs real audio/visual extraction with synthetic scores. `real`
requires the trusted trained bundle and never substitutes a mock classifier.
The separate `select_best_match_video_only(...)` compatibility function rejects
real mode. The production function has no optional audio argument.

## D. Audio implementation

Require float32 normalized PCM in [-1,1], exactly 80,000 samples at the fixed
16kHz contract. Accept `(80000,)` mono and `(80000,2)` time-major stereo; reject
missing/extra IDs, absent audio, other layouts/dtypes/lengths, nonfinite samples
and amplitude violations. Supplied digital silence is valid and distinct from
missing audio. Since no timestamps/rate field exists in the public API, upstream
must guarantee the actual sample rate and alignment with the video interval.

Lossless zlib transport preserves samples, pixels, order and dtypes. A v2
envelope binds its outer ID, video ID and audio ID. Bounded decoding rejects
corruption, truncation, trailing data and expansion bombs. The client verifies
returned IDs. Full-audio features require availability=1 in every interval.

The adapter calls the existing research method with `fps=15`, `audio=waveform`
and `audio_sample_rate=16000`. Columns 210:215 remain research-owned log RMS,
RMS delta, spectral flux, spectral centroid and availability. Stereo channels
are not downmixed; research channel-power and magnitude-spectrum handling is
preserved. No audio feature formula was independently implemented.

Preparation uses existing authorized broadcast sources, only seconds 120–125
of two distinct matches. Both have original `(75,720,1280,3)` RGB video and
`(80000,2)` audio, RMS **0.0551013** and **0.1047648**. Video samples retain
the source frame displayed at each interval centre.

The existing research file decoder produced mostly unavailable audio locally
when cropped endpoint frames and full MKV frames used different input timebases.
Its audio/video source files match the unchanged vendored wheel. The isolated
test preparer normalizes all frame timebases, verifies sample-clock continuity
against source timestamp quantization, crops before resampling, preserves
stereo and rejects discontinuities without padding. Two other candidate sources
were rejected. This does not change research source, jobs or audio feature math.
The training owner should check audio availability in their actual extraction
path before promoting the full-audio checkpoint; these observations do not
establish that the training job uses the same decoder path.

## E. Modal deployment

- App/environment: `matchpulse-inference` / `main`.
- `FeatureWorker.extract`, `FeatureWorker.diagnose`, `FeatureWorker.diagnose_pair`.
- `ClassifierWorker.ready`, `ClassifierWorker.predict`; CPU worker not invoked.
- Actual device: **Tesla T4**, active model `cuda:0`, `torch.float16`.
- Torch `2.11.0+cu130`, CUDA `13.0`, Python 3.13; pinned worker packages loaded.
- Existing verified YOLO26s weights and unchanged research wheel reused.
- Detector remains 640, batch 16, confidence 0.15/person and 0.08/ball,
  existing BoT-SORT, 15 FPS, five seconds.
- GPU maximum one container, serial inputs, zero warm/buffer minimum,
  five-second requested idle scale-down, 180-second execution/300-second startup limits.
- GPU worker CPU 1–4 cores / RAM 2–8 GiB; client in-flight limit one.
- CPU classifier: two cores, 4 GiB, maximum one, minimum zero.
- Independent Volume: `matchpulse-inference-models`; no trained bundle uploaded.

One deployment attempt succeeded in **9.49 seconds**, using the package module
name rather than a file-path import. Eight RPC inputs succeeded, with no failed
or timed-out inputs. Seven containers started sequentially; this is not seven
concurrent GPUs. Class method statistics share the same counts and were counted
once. No L4 request or research-app operation was made by this task.

Final independent inspection: `APP_STATE_DEPLOYED`, runners=0, running inputs=0,
backlog=0. Cached image, independent inference model Volume and provider-managed private
RPC artifacts remain. No public HTTP/footage endpoint exists. No full match-half
video was copied/uploaded, and raw frames/audio were never written into local
test artifacts or the model Volume. SDK input blobs follow provider retention.

## F. Numerical equivalence

Reference: fresh research `MatchFeatureExtractor` instances, with independent
tracking/caches and the same stateless detector, configuration, GPU, FP16,
ordered RGB arrays and exact raw waveform. Thus this compares wrapper/transport
equivalence within the same environment, not cross-GPU reproducibility or
agreement with a separately decoded file-audio path.

| Check | Match 01 | Match 02 |
|---|---:|---:|
| Reference and Modal shape | (75,215) | (75,215) |
| Dtype | float32 | float32 |
| Finite | Yes | Yes |
| Maximum absolute difference | 0.0 | 0.0 |
| Elements outside tolerance | 0 | 0 |
| Player slots / teams | Exact | Exact |
| Camera features / cuts | Agree / exact | Agree / exact |
| Audio columns | Exact | Exact |
| Audio availability intervals | 75/75 | 75/75 |
| Nonzero audio features | Yes | Yes |

Fixed tolerances: **atol=2e-5, rtol=1e-5**. No thresholds were weakened.
All 75 sampled frames were inferred for each diagnostic extraction. Returned
input hashes matched both modalities, neither input was mutated, and A–B–A was
exact in the same container. Returned tensor size is **64,500 bytes** per match.

## G. End-to-end validation

Every row below called the actual three-argument public function, validated
inputs, performed real Modal audio/video extraction, checked returned features,
then applied a deterministic synthetic scorer and staying bonus. The scorer
recorded every expected ID, preventing an all-failed fallback from passing.

| Case | Scores / behavior | Selected | Total method latency |
|---|---|---|---:|
| One match | Default 0.5 | match_01 | 35.728 s |
| Two distinct windows | Both 0.5; current gets bonus | match_01 | 66.427 s |
| Challenger wins | Current 0.4; challenger 0.8 | match_02 | 75.432 s |
| Staying bonus | Current 0.5 + 0.1; challenger 0.55 | match_01 | 77.895 s |

These are successful feature-extraction/mock-score decisions, not GRU predictions
or anticipation accuracy measurements. The shared application was not integrated.

## H. Performance

| Source preparation measurement | Match 01 | Match 02 |
|---|---:|---:|
| Raw RGB bytes | 207,360,000 | 207,360,000 |
| Compressed RGB bytes | 82,572,225 | 71,046,965 |
| Compressed/raw video | 39.82% | 34.26% |
| Stereo PCM bytes | 640,000 | 640,000 |
| Compressed audio bytes | 594,443 | 594,606 |
| Video compression | 8.509 s | 7.473 s |
| Audio encoding | 0.067 s | 0.070 s |
| Local decode/resample | 3.580 s | 3.853 s |

Decode/resample is preparation outside the public method, whose inputs are
already arrays. Across the seven public per-window extraction calls:

| Stage | Observed range |
|---|---:|
| Local video compression | 7.175–10.726 s |
| Local audio encoding | 0.065–0.082 s |
| SDK submission | 5.703–13.526 s |
| SDK result wait | 7.565–27.865 s |
| Remote bounded decompression | 0.899–1.051 s |
| Full extraction | 3.170–8.786 s |
| YOLO detector stage | 1.270–6.570 s |
| Tracking/visual analysis/interpolation/assembly | 1.486–2.190 s |
| Research audio features | 0.018–0.038 s |
| Worker initialization | 3.304–8.593 s |
| Original detector checkpoint construction | 0.231–0.403 s |

The first diagnostic worker initialized in 7.287 seconds; first extraction was
8.176 seconds, including 6.654 seconds in the detector stage. In that same warm
worker, subsequent complete extractions took **3.152 seconds (B)** and
**2.753 seconds (A)**, with detector stages about **1.27–1.28 seconds**. Caches
were cleared, so this is fresh detection with warm model state. Initial GPU
prediction setup/warmup belongs to the first detector stage, not the small
checkpoint-construction measurement. Detector timing includes preprocessing,
postprocessing and transfers; it is not isolated CUDA-kernel time.

The A–B–A validation RPC took 61.731 seconds, including two fresh reference
extractions. Its 35.603-second nonexecution residual combines submission/upload,
scheduling, initialization and return. Public wait-minus-remote-wall residuals
were 3.496–18.544 seconds. Pure upload, scheduling and return-transfer timings
could not be independently isolated through the SDK; they are not invented or
equated to those residuals. The provider reports startup metrics too, but these
few observations support no general p95/p99 claim.

The measured path fails the **3–10-second** anticipation requirement, even
before an actual GRU is evaluated. RGB encoding and submission alone often
exceed ten seconds per match. Audio and synthetic scoring are negligible
relative to video transport; scorer time was 0.20–1.08 ms. Repeated cold starts
also matter: seven containers served eight RPCs under short idle scale-down.

The smallest next step is to benchmark from the production ingestion host near
the GPU, reducing raw-array WAN transfer. Then compare bounded parallel frame
compression / faster lossless codecs with exact round-trip checks. A longer
bounded idle window may reduce repeat initialization, but warming alone will
not remove measured encoding/upload costs. No resizing, lossy codec, frame
skipping, feature changes or complex cache was introduced. Seven-match testing
was deferred because the one/two-window latency already misses the horizon;
no seven-stream capacity is claimed.

## I. Costs

- Additional conservative estimate: **$0.7158082093**.
- Build/deployment allowance retained: **$0.60**.
- Eight successful GPU RPC allowances settled to modeled cost: **$0.1158082093**.
- Earlier task estimate retained separately: **$2.8250928**.
- Combined inference estimates: **$3.5409010093**.

The **$5 additional task ceiling was respected**; even the combined retained
estimates are below $5. Rates used: T4 $0.000164/s, CPU $0.0000131/core/s, RAM
$0.00000222/GiB/s ([Modal pricing](https://modal.com/pricing)). The model uses
the requested maximum four cores/eight GiB and includes 30 seconds of idle
allowance per call; client upload/wait time makes this conservative.

Observed remote execution wall time sums to **82.989 seconds**, including
reference work. Unique-container initialization sums to **35.883 seconds**.
These are activity measurements inside GPU containers, not total billed GPU
lifetime or pure GPU compute. Provider-billed GPU seconds/dollars and an invoice
were unavailable; no precise actual charge or absence of unexpected charges is
claimed. No further paid work ran after the bounded tests. Research spending
and its separate budget were not queried or altered.

## J. Tests

- **159 passed, zero failed, zero skipped**, including the original 113 cases
  adapted to the new contract/explicit legacy path and 46 additional cases.
- Ruff: all checks passed with `--no-cache --isolated`.
- Python 3.10 syntax: **26 files** passed.
- Executable three-argument fast-mock example passed.
- Actual cloud gates: two full-audio reference comparisons, same-worker A–B–A,
  four public-method cases and final idle/lifecycle inspection passed.
- Protected-file hash changes: **0 of 93**; branch/HEAD unchanged.

Ordinary tests need no Modal credentials, CUDA, footage, checkpoint or model
download. Covered: mono/stereo/silence, missing/invalid audio, ID mismatch,
wrong lengths/rate metadata, nonfinite/range violations, exact transport,
corruption/expansion bounds, returned-ID mismatch, research waveform delegation,
full-channel classifier readiness, deterministic scoring and worker failures.

Unverified: actual trained GRU predictions/quality, production-host latency,
seven distinct-match throughput, shared-app integration and prediction lead time.
Numerical equivalence on two samples is not a model-accuracy evaluation.

## K. Remaining work and exact backend handoff

1. Receive a trusted exported **full-audio GRU portable bundle**, not training-only
   weights or the 210-channel variant. The training owner should verify audio
   availability in their actual feature-extraction path.
2. Upload it to a versioned path in `matchpulse-inference-models`, set
   `MATCHPULSE_ML_MODE=real` and `MATCHPULSE_ML_CHECKPOINT=v1/classifier.pt`.
   CPU readiness validates the 215-channel schema before paying for extraction.
   Research loading owns saved normalization, channel selection and batch prediction.
   Example upload, once the artifact is available:
   `python -m modal volume put matchpulse-inference-models C:/trusted/export/classifier.pt v1/classifier.pt`.
3. The backend teammate must produce synchronized snapshots of
   `current_match_id: str`, `match_frames: dict[str,np.ndarray]` and
   `match_audio: dict[str,np.ndarray]`. Deliver exactly 75 RGB frames at interval
   centres and 80,000 float32 samples from the same five-second source-PTS window.
   Keep stereo as `(80000,2)` when present; reject gaps instead of padding.
4. In the current backend/Docker working directory, import
   `from ml_inference import select_best_match` and call
   `selected = select_best_match(self.current_match_id, match_frames, match_audio)`
   inside the existing processing worker. From repository-root code the import
   is `backend.ml_inference`. Add the client's Modal dependency when enabling it.
5. Fix RGB/BGR handling and reversed window timestamps upstream, disable the
   unconditional dummy cam-1 switch, return the chosen camera's window metadata,
   and update current_match_id only when the ordered switch is accepted. Reject
   stale decisions by age/window ID. `README.md` contains the exact return snippet.
6. Reduce and remeasure latency from the production host before demonstrating
   timely automatic selection. Then validate actual model scores and obtain a
   bounded seven-distinct-match benchmark. No shared backend/frontend files were
   changed to simulate this integration.
