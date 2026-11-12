# Architecture

Touchline replays seven prerecorded matches. The structure refactor packages the
existing implementation without changing its inference, switching or playback
contracts.

## Runtime boundaries

| Area | Responsibility |
|---|---|
| `frontend/src/app/` | Application shell, theme, navigation and ordered stylesheet entry point |
| `frontend/src/features/watch/` | Watch view with session, playback, audio, director and source modules |
| `frontend/src/features/captions/`, `tables/`, `news/` | Caption rendering and supporting pages |
| `frontend/src/shared/` | Backend connections, shared connection events, reusable UI and common styles |
| `backend/src/touchline/replay/` | Source registry, replay session, clock permits and director policy |
| `backend/src/touchline/api/` | HTTP and WebSocket transport, application routes and bounded state delivery |
| `backend/src/touchline/hosted/` | Independent visitor sessions, capability/resume protocol and durable paid-work admission |
| `backend/src/touchline/deployment/` | Single-container Modal CPU hosting and isolated caption image |
| `backend/src/touchline/storage/` | Refreshable AWS credentials and expiring private S3 authorization |
| `backend/src/touchline/ml_inference/` | Modal bridge, persistent workers, cached extraction and model scoring |
| `backend/src/touchline/captions/` | Focused-match audio and the isolated ElevenLabs subprocess |
| `backend/src/touchline/football_platform/` | Server-side football-data.org and Guardian requests |
| `backend/src/touchline/tools/` | Current cloud/model operations, validation, evaluation and retained legacy workflows |

The browser and inference workers read the recordings independently from object
storage. Persistent workers cache incremental YOLO observations, complete
five-second audio/video windows at 15 FPS, and produce raw `(75,215)` features on
a two-second stride. The selected temporal GRU runs on CPU inside those workers
using its released `shots3_10` head. Compact scores travel over private worker
WebSockets to the CPU backend, which sends director state to the browser.

T4/L4 capacity fallback changes worker resource availability while preserving
the frozen model, raw `(75,215)` windows, seven-source assignment and source/clock
contracts. L4 is now preferred with T4 fallback, with eight preloaded replay
containers kept warm for two simultaneous visitors and one warm CPU API owner.
A public browser run used three T4 workers and one L4 worker. Each
20-minute Director run reserves $2.20, and independently admitted captions reserve
$0.14. Visitors can run concurrently, with ten combined starts per IP per rolling
hour and no cooldown, under the [hosted admission limits](modal-backend.md#configuration-and-durable-admission).

The backend validates timestamps, freshness, sequence, model/session identity and
clock epochs. Actual browser playback grants inference permits. Pauses, seeks,
stalls and disconnections update those permits; bounded queues and finite leases
limit work. A ready-player acknowledgement commits the focused match. All seven
video elements remain mounted and synchronized; only the focused player is
audible, with cross-tab audio ownership.

## Frontend composition

`App` composes `AppShell`, the Watch view and the supporting pages. `useTheme`
owns the saved theme and document colors; `useNavigation` owns hash/history
navigation. `useWatchReplay` is called unconditionally from `App`, and `WatchView`
is always rendered. Navigation changes `hidden`/active state rather than mounting
a different player tree, preserving all seven media elements and their clocks.

The Watch session coordinator routes backend messages and composes
`playback/usePlayerHandoff`, `director/useDirectorControls`, caption state and
audio ownership. Player handoff owns ready-player promotion and acknowledgement;
director controls own mode requests and authoritative confirmation. Pure director
view-state derivation stays separate from those lifecycles. Source registration
and verified replay-score metadata live under `watch/sources/`.

Feature styles are colocated with their code. `app/styles.css` imports contiguous
sections in the original rule order; application-level responsive/accessibility
files retain the cross-feature overrides at their original cascade positions.
The older `MainPlayer` and `VideoTile` components remain under
`watch/compatibility/` for existing contract tests and are outside the application
import graph. The unreferenced earlier grid/focus screens have been removed.

## Session ownership and director policy

The local launcher has one shared in-memory replay session per backend process
and one controlling viewer; other connections can be viewing-only. The public
Modal deployment instead creates an independent `ReplaySession` for each visitor,
with all seven recordings starting at zero and a per-tab resume capability.
The frontend must set `REACT_APP_INDEPENDENT_SESSIONS=true` to use that hosted
hello/resume protocol. Session/rate limits and durable paid-work admission apply
before provider work. Visitor state stays in one CPU container; scaling CPU
processes alone does not establish session routing or shared state. See
[hosted backend setup](modal-backend.md).

`replay/session.py` remains the session owner. `config.py` validates session
settings, `diagnostics.py` filters worker health, and `captions.py` and
`monitoring.py` coordinate their respective lifecycles against that same session.
These modules do not create additional sessions or change the playback clock.
`director.py` and `sources.py` retain switching policy and source registration.

`api/http.py` and `api/websocket.py` own browser transports; `api/routes.py`
assembles application routes, including saved captions. Football provider
requests, parsing, cache/quota handling and catalog search remain together in
`football_platform/`.

| Mode | Cooldown | Staying bonus | Fresh challenger confirmations |
|---|---:|---:|---:|
| Relaxed | 20 s | 0.25 | 3 |
| Balanced | 12 s | 0.20 | 2 |
| All Action | 4 s | 0.05 | 1 |

Manual selection holds for 30 seconds. Confirmations must come from fresh,
distinct predictions for the same challenger. Switching policy does not alter
the two-second inference stride.

## Caption environment

The CPU backend and ElevenLabs SDK require different WebSocket versions, so
captions run in a separately installed Python environment. The backend launches
`touchline.captions.live.worker` using `MATCHPULSE_CAPTION_PYTHON` and exchanges
bounded JSON messages through the subprocess pipes. The same backend package
must be installed in both environments; their third-party dependencies stay
separate. See [backend setup](../backend/README.md#dependency-environments).

Only the focused recording's audio is sent progressively from its actual
playback position. Partial transcripts are replaced by committed text. Switching,
pausing, seeking, buffering and disconnecting cancel or update the audio source;
turning captions off stops paid audio. Permanent ElevenLabs and football-provider
keys remain server-side. Caption and inference usage accounting remain separate;
the public deployment also requires a shared bounded admission reservation before
either paid path can start.

`captions/live/` separates the backend adapter, SDK connection, transcription
engine, process entry point, progressive audio reader and event handling.
`captions/tracks/` contains the optional bounded saved-track pipeline, its CLI,
provider adapter, reader and schema. Source identity, errors and usage limits
are shared at the caption package root. Live playback does not invoke the
saved-track generation pipeline.

## Package data and writable paths

`backend/pyproject.toml` installs source from `backend/src/touchline/`. Tests live
outside that package under `backend/tests/unit/` and `backend/tests/integration/`.
Reusable synthetic builders and provider doubles live in domain groups under
`backend/tests/support/`; fixture data remains under `backend/tests/fixtures/`.
The backend wheel includes the canonical fixture catalogue plus audited inference
runtime wheels, release manifests, decoder support and smoke fixtures. Model
weights, source recordings, credentials, environments and generated receipts
are separate deployment/local inputs.

`backend/src/touchline/data/matches.json` is the only maintained fixture catalogue.
The Python package reads it locally. Before frontend start, test or build,
`scripts/sync-match-catalog.cjs` copies its exact bytes into ignored
`frontend/src/data/matches.json`, which CRA can import. Match IDs, source bindings
and playback offsets retain their existing meaning.

`touchline.paths` locates the repository root for editable installs. A wheel
installation defaults to its working directory; `MATCHPULSE_PROJECT_ROOT` sets an
explicit configuration/artifact root. The local launcher reads the root `.env`.
Writable output is under `backend/artifacts/`, including inference receipts and
private credentials under `backend/artifacts/inference/`. Package code and data
can therefore remain separate from writable runtime state.

The inference source is grouped into `features/`, `models/`, `streaming/`,
`deployment/` and `window_api/`. Frozen release assets stay in `runtime/`;
deployment mounts the complete inference package. Operational commands share
environment and receipt helpers, while retaining their distinct ledger policies
and historical artifact directories. See [inference](inference/README.md).

Current operations are grouped under `tools/cloud/`, `tools/validation/` and
`tools/evaluation/`. `tools/support/` owns the shared helpers, and `tools/legacy/`
retains the earlier explicit validation/browser workflows.

The public frontend is [hosted on Vercel](https://touchline-football-director.vercel.app)
and uses [the Modal CPU backend](https://coshea46--touchline-backend.eu-west.modal.run).
Existing Modal GPU workers, the owner's private S3 recordings and isolated
ElevenLabs caption runtime retain their roles. Verification on 12 November 2026
passed HTTP/S3 checks, seven-source browser decoding and manual switching with
preserved nodes, nonzero reload resume, independent visitor WebSocket isolation
and focused Scribe partial/committed delivery. A separate bounded T4 operator check
verified complete real-model inference cohorts across all seven sources. The
final public browser run used T4/L4 workers, observed automatic switching and
focused captions, and confirmed cleanup. Its strict repeated seven-source score
cohort check failed; sustained scoring in that joined path remains unverified.
The hosted worker connection
deadline is 110 seconds; the generic client default remains 90 seconds and the
public Director and caption runs are independently capped at 1200 seconds. Cold provider queueing can outlast these
startup bounds; uncertain cleanup fails closed pending operator reconciliation.
See [hosted verification status](modal-backend.md#frontend-connection-and-verification).
Existing `MATCHPULSE_*` variables and `matchpulse` resource/runtime names remain
compatibility identifiers.

See [inference](inference/README.md) for the worker contract and operational tools,
and [the root README](../README.md) for model results, attribution and limitations.
