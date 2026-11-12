# Modal CPU backend

The website's CPU deployment is separate from the existing GPU inference app:

```powershell
& backend/.venv/Scripts/python.exe -m modal deploy -m touchline.deployment.modal_backend
```

This command builds and deploys the public endpoint. Importing the deployment
definition or running its offline tests does not deploy anything. Use the URL
returned by Modal rather than deriving a hostname from the workspace ID.

The current frontend is [Touchline on Vercel](https://touchline-football-director.vercel.app).
Its public CPU API is [the Modal backend](https://coshea46--touchline-backend.eu-west.modal.run),
with WebSocket transport at `wss://coshea46--touchline-backend.eu-west.modal.run/ws`.

## One container, independent visitor sessions

`touchline-backend` exposes one ASGI endpoint for HTTP APIs and `/ws`. Its native
ASGI application owns separate replay state for each visitor. A connection begins
with a bounded hello/resume exchange; resume capabilities belong to one visitor,
and a missing session after a cold restart fails closed rather than joining
another viewer's replay. Session creation does not start paid AI.
Each new visitor starts all seven recordings at zero; the browser keeps its
resume capability per tab. The default limits are ten independent sessions,
twenty minutes per session and a sixty-second reconnect grace period.

The deployment uses these limits:

| Setting | Value |
|---|---:|
| Maximum CPU containers | 1 |
| Minimum and buffer containers | 0 |
| Idle scale-down window | 60 seconds |
| Concurrent Modal inputs | 100, target 50 |
| CPU request / maximum | 0.25 / 1 core |
| Memory request / maximum | 512 / 1024 MiB |
| Input timeout | 3600 seconds |
| Automatic retries | Web-function default (none) |

An open WebSocket is a Modal input. Visitor limits and finite session lifetimes
are enforced inside the application, below the infrastructure input limit.
Disconnected visitors have a short reconnect grace period; unused sessions and
paid resources are cleaned up. ASGI lifespan shutdown closes all sessions and
shared API services. Active sockets keep the container running; scale-to-zero
applies after requests end and the idle window passes.

The one-container limit is deliberate: visitor state and admission locks live in
memory. Raising it requires shared session routing/state and a compatible durable
admission design. This deployment does not silently become a distributed service.
Container restarts can lose visitor replay state even though admission history
persists. Drain active sessions before changing deployment ownership or state
mounts.

Traffic is routed through Modal's `eu-west` gateway. Container placement is left
at the provider default; this avoids selecting a placement region with an added
region multiplier. `AWS_DEFAULT_REGION=eu-west-2` independently identifies the
recording bucket's AWS region.

## Configuration and durable admission

The deployment references existing resources:

- Modal Secret `touchline-backend-config` supplies server-side configuration.
- Existing Modal Secret `matchpulse-inference-replay-auth` supplies the private
  inference worker authentication value.
- Modal Volume `touchline-backend-state` mounts at `/app/state`.
- Admission state is stored in `/app/state/admission.sqlite3`.

The references do not create either resource. Provision them explicitly before
deployment. `MATCHPULSE_BACKEND_SECRET_NAME` can select another existing secret
when evaluating the deployment definition. Secret values, `.env`, local artifacts
and credentials are excluded from image inputs.

The server factory maps `MATCHPULSE_REPLAY_WS_TOKEN` from the existing inference
secret to `MATCHPULSE_MODAL_WS_TOKEN` only when the latter has no value. An explicit
backend token takes precedence. The worker WebSocket endpoint belongs in
`MATCHPULSE_MODAL_WS_URL` in backend configuration. Neither value is returned to
the frontend, and reusing the secret does not redeploy the GPU application.

At container startup the factory reloads the state volume, then constructs
`AdmissionPolicy` with its persistent database and the volume's commit callback.
The policy must commit a paid reservation before permitting provider work; a
filesystem write alone is insufficient for a volume-backed cold restart. Its
limits, cooldowns and conservative handling of uncertain cleanup apply across
visitors. New public AI grants reserve $0.70 for up to 120 seconds. The deployed
$3.30 UTC-day allowance permits at most four new grants on a fresh day, subject
to the $3.30 total allowance, one admitted AI session at a time and
one grant per IP per hour. The generic `AdmissionPolicy` code fallback remains
$1.20 per UTC day (at most one new $0.70 grant) when no explicit daily setting is
supplied. Reservations are non-refundable;
changing the configured allowance does not refund reservations or reset cooldowns.
The total allowance does not reset at midnight or on a cold start. These are
application estimates and admission limits, not a provider-enforced billing cap.

The deployed backend uses these explicit nonsecret settings:

```dotenv
TOUCHLINE_AI_DAILY_BUDGET_USD=3.30
TOUCHLINE_AI_TOTAL_BUDGET_USD=3.30
MATCHPULSE_MODAL_WS_CONNECT_TIMEOUT=110
```

The four public verification reservations remain charged at their original
amounts ($0.60, $0.60, $0.70 and $0.70), totaling $2.60. The final committed-ledger
audit confirmed all four leases closed without refunding any reservation. One
$0.70 visitor grant remains within both current allowances, subject to the
daily/IP limits. These reservations and two separate $0.60 operator reservations
leave $0.50 of the owner's $5 verification allocation for CPU/storage overhead;
they are conservative estimates, not a reconciled provider bill.

The hosted worker connection deadline is 110 seconds; the generic client default
remains 90 seconds. This setting changes connection startup only. The public AI
permit remains capped at 120 seconds and is not extended by slow startup.

An uncleared paid lease blocks new starts even after its timestamp expires or
the container restarts. Confirmed cleanup releases the active slot; uncertain
cleanup requires operator inspection and deliberate remediation. Resetting the
volume would also erase the total-usage protection.

The base image disables automatic monitoring and captions. Visitors start with
Auto and captions off. A configured server-side policy and the applicable
credentials are required before explicit visitor requests can start bounded AI.
Never put permanent provider keys or the private inference token in frontend
environment variables. The separate $5 deployment-verification allowance is not
the website's daily admission limit.

The secret must contain nonempty `MATCHPULSE_ALLOWED_ORIGINS` with the exact
frontend origin(s) for HTTP and WebSocket admission, plus any provider configuration intentionally enabled for
the public demo. A public endpoint does not need a permanent Modal proxy token in
the browser. Visitor capability tokens travel through the application's protocol
and should not be logged or embedded in URLs.

## Image and caption interpreter

The image builder copies only the backend package source, package metadata,
README and caption requirements. It builds one backend wheel, installs it into
the CPU interpreter, and installs that same wheel with `--no-deps` into
`/opt/matchpulse-captions`. The isolated interpreter receives its separately
pinned ElevenLabs, WebSocket, PyAV and NumPy dependencies.

`MATCHPULSE_CAPTION_PYTHON` points to
`/opt/matchpulse-captions/bin/python`; the backend starts
`touchline.captions.live.worker` there only after admission. CPU and caption
WebSocket dependencies remain separate. The existing GPU deployment,
`touchline.ml_inference.deployment.modal_app`, its model volume and worker
contracts are unchanged.

The inference deployment allows T4/L4 capacity fallback while retaining the
frozen selected model, seven source readers, raw `(75,215)` feature windows and
the existing playback-permit/clock contract. GPU availability remains a provider
constraint. The final public browser run used three T4 workers and one L4 worker;
this was confirmed separately from provider container metadata. The earlier
successful operator result below was measured on T4.

Recordings stay in private S3 and are read directly by authorized consumers.
The CPU image includes packaged source metadata and frozen inference support
assets, but no local recordings, environments or ignored model weights. See
[private S3 access](private-s3.md) for OIDC, expiry and GET/HEAD authorization.

## Frontend connection and verification

Set all three public build settings in Vercel:

```dotenv
REACT_APP_API_URL=https://coshea46--touchline-backend.eu-west.modal.run
REACT_APP_WS_URL=wss://coshea46--touchline-backend.eu-west.modal.run/ws
REACT_APP_INDEPENDENT_SESSIONS=true
```

`REACT_APP_INDEPENDENT_SESSIONS=true` selects the hosted hello/resume protocol;
the local shared-session backend uses its existing protocol. These frontend
settings contain no provider credentials. Backend origin admission and S3 CORS
must include the exact frontend origin.

The Vercel frontend and CPU backend are deployed. Verification on 12 November
2026 passed HTTP health/API checks, all seven private fixture bindings, signed
HEAD requests, one-byte HTTP 206 ranges and production-origin CORS. The public
browser decoded all seven recordings and advanced the native playback clock.
Manual selection across all seven preserved the video nodes, with a maximum
clock deviation of 0.002 seconds; same-tab reload resumed the nonzero replay.
Separate backend WebSocket checks verified two independent visitors starting at
zero, isolated controls and capability-based resume. The storage receipt does
not establish full-file checksum equality.

Real focused Scribe verification produced 82 partial and 20 committed events;
transcript text was not persisted. A separate bounded operator check verified
the frozen real model on T4 across all seven sources, with complete score cohorts
at observation ends of 5 and 7 seconds. Startup took 21.415 seconds and the check
finished in 30.746 seconds, with owned GPU stop and source cleanup confirmed and
no errors.

The final public Chrome check admitted one ordinary visitor, decoded all seven
recordings, enabled Auto through the UI, received real-model recommendations,
observed an automatic source switch and advanced native playback by 90.656
seconds while AI was enabled. Focused Scribe produced 74 valid partial and 16
committed events. Turning captions and Auto off received the stopped lifecycle
acknowledgement. The subsequent committed-ledger audit confirmed the permit
closed, and backend/GPU statistics confirmed no queued or running inputs or
containers. There were no page, media, socket, server-frame or CORS errors.

The strict browser check nevertheless failed: it recorded four recommendations
but retained only one complete seven-source score cohort, at observation end
9 seconds, instead of the required two advancing cohorts in one clock epoch.
Its cohort recorder resets history on an epoch change, so this receipt alone
cannot distinguish lost earlier evidence from a sustained score-cadence problem.
Automatic switching and captions have live evidence; sustained seven-source
scoring in the joined public browser path remains unverified. No further paid
verification was started after this result.

An unpaid regression investigation subsequently reproduced a separate browser
clock bug: a network `stalled` event cleared playback readiness even when the
native video retained buffered future data and kept playing. The player now
preserves the actual clock in that case; `waiting`, seeking and insufficient
readiness still revoke playback permits. Regression tests also verify that
automatic handoff keeps reporting the promoted video's advancing native time.
This fix has not been rechecked with paid AI, and its role in the live score
shortfall is unproven.

Cold provider queueing exceeded the earlier 90-second WebSocket `open_timeout`:
the public AI probe encountered about 906 seconds of queueing before its four
GPU requests later finished. This provider delay remains an operational limit.
A public browser AI attempt also exceeded that timeout while four T4 requests
queued for about 176 seconds. It did not verify the joined public Auto path.
An uncertain cleanup fails closed and requires operator reconciliation before
further paid starts; a successful bounded operator check does not guarantee
public cold-start latency.

The deployment-definition tests use a fake Modal SDK and make no cloud calls:

```powershell
& backend/.venv/Scripts/python.exe -B -m pytest -c backend/pyproject.toml backend/tests/unit/deployment
```

Before deployment, also run the backend suite and the
[installed-package verifier](../scripts/README.md#offline-package-verification).
After deployment, verify cold start, independent visitor controls, reconnect
expiry, signed media renewal, tables/news and admission rejection before any
separately authorized paid AI check. Keep the verification allowance bounded and
do not interpret a successful package build as proof of public playback or
provider latency.

The implementation follows Modal's documented [ASGI/WebSocket support](https://modal.com/docs/guide/webhooks),
[input concurrency](https://modal.com/docs/guide/concurrent-inputs), and
[routing/region selection](https://modal.com/docs/guide/region-selection).
