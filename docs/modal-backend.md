# Modal CPU backend

**Current deployment (13 November 2026):** both CPU and GPU minimum container
counts are zero, with no warm buffer and 30-second scale-down. The startup
tutorial and bounded paused initialization replace the earlier always-on floor.
The current testing reservation cap is $50.42 cumulative, retaining $40.42 of
earlier reservations. The $17.80 UTC-day cap retains $7.80 already reserved that
day plus the owner's new $10 testing allowance. See [startup flow](startup-tutorial.md).
Earlier warm-capacity and $100 settings below describe the prior deployment.

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
an initial twenty-minute replay lifetime and a sixty-second reconnect grace period. Starting
a paid run extends that visitor's expiry through the run deadline plus reconnect
grace, so a late start still receives its full allowance.

The deployment uses these limits:

| Setting | Value |
|---|---:|
| Maximum CPU containers | 1 |
| Minimum / buffer CPU containers | 1 / 0 |
| Idle scale-down window | 60 seconds |
| Concurrent Modal inputs | 100, target 50 |
| CPU request / maximum | 0.25 / 4 cores |
| Memory request / maximum | 512 / 4096 MiB |
| Input timeout | 3600 seconds |
| Automatic retries | Web-function default (none) |

An open WebSocket is a Modal input. Visitor limits and finite session lifetimes
are enforced inside the application, below the infrastructure input limit.
Disconnected visitors have a short reconnect grace period; unused sessions and
paid resources are cleaned up. ASGI lifespan shutdown closes all sessions and
shared API services. One API container stays warm to avoid API cold starts.

The one-container limit is deliberate: visitor state and admission locks live in
memory. Raising it requires shared session routing/state and a compatible durable
admission design. This deployment does not silently become a distributed service.
Container restarts can lose visitor replay state even though admission history
persists. Drain active sessions before changing deployment ownership or state
mounts.

API traffic is routed through Modal's `eu-west` gateway, and its CPU container
is placed in `eu-west`. Replay GPU containers use the broader `eu` placement
region to stay near the London recordings while retaining a larger capacity
pool. Modal's placement multipliers apply: 1.75x for the CPU's narrow region and
1.15x for the GPU's broad region. `AWS_DEFAULT_REGION=eu-west-2` independently
identifies the recording bucket's AWS region. The existing private GPU WebSocket
ingress retains its original routing region.

## Configuration and durable admission

The deployment references existing resources:

- Modal Secret `touchline-backend-config` supplies server-side configuration.
- Existing Modal Secret `matchpulse-inference-replay-auth` supplies the private
  inference worker authentication value.
- Modal Volume `touchline-backend-state` mounts at `/app/state`.
- SQLite admission state is stored in `/app/state/admission.sqlite3` by default.
- Explicit Dynamo mode uses an existing table named by
  `TOUCHLINE_ADMISSION_DYNAMODB_TABLE`; it performs no legacy-volume I/O.

The references do not create either resource. Provision them explicitly before
deployment. `MATCHPULSE_BACKEND_SECRET_NAME` can select another existing secret
when evaluating the deployment definition. Secret values, `.env`, local artifacts
and credentials are excluded from image inputs.

The server factory maps `MATCHPULSE_REPLAY_WS_TOKEN` from the existing inference
secret to `MATCHPULSE_MODAL_WS_TOKEN` only when the latter has no value. An explicit
backend token takes precedence. The worker WebSocket endpoint belongs in
`MATCHPULSE_MODAL_WS_URL` in backend configuration. Neither value is returned to
the frontend, and reusing the secret does not redeploy the GPU application.

In the default `TOUCHLINE_ADMISSION_STORE=sqlite` mode, the factory reloads the state volume, then constructs
`AdmissionPolicy` with its persistent database and the volume's commit callback.
The policy must commit a paid reservation before permitting provider work; a
filesystem write alone is insufficient for a volume-backed cold restart. Its
limits and conservative handling of uncertain cleanup apply across visitors.
`TOUCHLINE_ADMISSION_STORE=dynamodb` instead selects atomic DynamoDB transactions,
removing volume commits from the visitor admission path. It requires an audited
migration of the complete existing ledger and a ready-marker check at startup;
it never silently falls back to SQLite. See the
[DynamoDB schema and migration procedure](admission-dynamodb.md) before selecting
that mode. No funding or historical amounts change when switching stores.
Director and captions have independent 1200-second leases and controls. Director
starts reserve $2.60 for up to four T4/L4 workers, including bounded startup and
the GPU's 15% regional placement premium. Historical $2.20 reservations retain
their original amounts. The reservation covers active processing and bounded
cleanup; caption starts reserve $0.14 for focused Scribe audio. Both count toward
ten starts per IP in a rolling hour. There is no cooldown or global active-run
mutex. Repeated On while a component is active does not reserve again or renew
its deadline. Off stops only that component; after confirmed cleanup it may be
started again with a fresh lease, subject to the hourly and spending limits.

The owner initially authorized $10 of additional available public-demo allowance,
giving a $12.60 cap with the earlier $2.60 reservations. The later authorization
to prioritize startup speed over cost enables a bounded $100 cumulative/UTC-day
application reservation cap, with all historical charges retained. Warm GPU/API
containers are billed independently of this per-run ledger, including while idle;
the application cap does not stop those infrastructure charges.
At the observed eight-T4 floor, GPUs alone are approximately $5.43/hour
($130.36 per continuous day), calculated from [Modal's T4 price](https://modal.com/pricing)
and the [1.15x broad-region multiplier](https://modal.com/docs/guide/region-selection).
CPU, memory, storage and data transfer are additional; L4 replacement capacity
would cost more. Keeping this floor running is intentional under the owner's
cost waiver and is not covered by the $100 application reservation limit.
The generic code fallback remains $1.20/day and $3 total if no explicit
funding is supplied; that fallback cannot admit a $2.60 Director run.
Reservations are non-refundable; changing the configured allowance does not
refund reservations or erase start counts.
The total allowance does not reset at midnight or on a cold start. These are
application estimates and admission limits, not a provider-enforced billing cap.

The deployed backend uses these explicit nonsecret settings:

```dotenv
TOUCHLINE_AI_DAILY_BUDGET_USD=100.00
TOUCHLINE_AI_TOTAL_BUDGET_USD=100.00
TOUCHLINE_ADMISSION_STORE=dynamodb
TOUCHLINE_ADMISSION_DYNAMODB_TABLE=touchline-admission
AWS_ROLE_ARN=arn:aws:iam::709860105019:role/TouchlineBackendRuntime
MATCHPULSE_S3_MEDIA_ORIGINS=split
MATCHPULSE_MODAL_WS_CONNECT_TIMEOUT=110
```

The four public verification reservations remain charged at their original
amounts ($0.60, $0.60, $0.70 and $0.70), totaling $2.60. The final committed-ledger
audit confirmed all four leases closed without refunding any reservation. No
reset was performed. The additional $10 is public-demo funding, separate from
the earlier $5 verification allocation. The admission ledger reserves estimated
AI costs; CPU hosting and S3 traffic remain separate provider charges, and these
figures are not a reconciled provider bill.

The hosted worker connection deadline is 110 seconds; the generic client default
remains 90 seconds. This setting changes connection startup only. The public AI
run remains capped at 1200 seconds and is not extended by slow startup.

An uncleared lease blocks that visitor's same component, even after expiry or
restart. It does not block another visitor or the other component. Confirmed
cleanup closes only its opaque permit ID. Uncertain cleanup requires operator
inspection; resetting the volume would also erase total-usage protection.
Historical permits retain their original amounts and schema rows. A separate
mapping table binds new permit IDs to visitor/component ownership.

The base image disables automatic monitoring and captions. Visitors start with
Auto on and captions off. The backend admits startup once after the first actual
ready playback report; neither construction nor hello alone starts a GPU. An
explicit Off before media readiness cancels that startup intent. Reconnects do
not silently grant another run. A configured policy and applicable credentials
are required before either component can start paid work independently.
The default startup counts as one Director start. Existing `ai_*` snapshot
fields describe only the Director; caption intent, status and deadline remain
under `captions`. Admission errors identify their `feature`, so a caption limit
cannot switch the Director off or change its reservation.
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
contracts are unchanged. The ReplayWorker pool can scale to 40 containers for ten
visitors, while each individual Director run remains bounded to four workers.
Eight replay GPU containers stay warm for two simultaneous visitors; overflow
containers can remain idle for up to 300 seconds. L4 is preferred with T4 as
fallback. There is no additional GPU buffer floor. Other worker pools retain
their existing zero minimums. Provider/account capacity can still restrict scale.
Caption-only runs never create an inference bridge or GPU input.

Each warm replay container loads and warms YOLO, then preloads and validates the
exact selected temporal GRU before accepting a session. No synthetic prediction
is used as evidence. Per-visitor readers, feature caches and playback permits stay
independent. Private-source authorization overlaps up to four read-only S3 HEAD
checks; fresh identity validation, credential-bound signing, ordered publication
and cleanup remain enforced. The API warms one app-owned S3 access resource at
startup. Visitors borrow its refreshable credentials and a frozen-generation
signer, avoiding repeated STS exchanges and client construction. Every visitor
still receives fresh object HEAD verification and correctly expiring GET/HEAD
signatures. Borrowed native work must finish before app-owned clients can close.
Registry metrics record counts and elapsed times without source keys or signed
URLs. Independent video and audio containers open in parallel with joined failure
cleanup, while seek, priming and factual playback watermark rules remain intact.
The frontend gives the focused recording first access to network/decoder resources
before starting untouched previews; all seven players remain mounted and any
already-started preview remains warm.

Private browser media alternates four regional and three dual-stack S3 URLs
when `MATCHPULSE_S3_MEDIA_ORIGINS=split`. Both are independently SDK-signed for the
same configured bucket and key. The [AWS dual-stack endpoint](https://docs.aws.amazon.com/AmazonS3/latest/developerguide/dual-stack-endpoints.html)
supports IPv4 and IPv6. This gives HTTP/1.1 browsers two origins instead of
queuing a seventh long video download behind six occupied connections. Canonical
worker GET, caption HEAD/GET, verified object digest, offsets and fixture metadata
remain unchanged. The shared authorization deadline is the earliest of all
published signatures. Default `single` mode remains available; unsupported split
adapters or mismatched alternate locators fail closed.

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

**Latest deployed result:** consistent five-second startup is not established.
The fourth strict warm browser check measured Director On at **6.743 and 7.426
seconds from navigation**, or **2.143 and 2.759 seconds after factual native
playback readiness**. First complete seven-source model cohorts arrived at
17.054 and 19.278 seconds; both visitors had overlapping real inference and
advancing cohorts without page, socket, server-frame or reconnect errors.
The source-origin fix was confirmed: each visitor received all seven HTTP 206
responses across four regional and three dual-stack connections, and all seven
videos obtained metadata. The former indefinitely queued seventh request was
resolved. Only five and two decoders, respectively, produced frames before the
bounded test ended; previews were awaiting native seeks, and the focused clock
also stalled. Bandwidth/decoder pressure is consistent with these observations,
but its precise contribution is not isolated. The full browser receipt remains
failed. Earlier faster readiness measurements below do not override this result
or guarantee five-second navigation startup, cold starts or overflow capacity.

One fourth-check session did not confirm its remote stopped lifecycle. Its
reservation correctly remained open after UI Off, and the first ledger audit
failed that cleanup condition. The operator stopped the old CPU and GPU apps,
verified both old applications had zero tasks, then transactionally closed only
that exact Director permit and its matching owner. All other records, including
the two earlier unresolved historical permits, remained unchanged; no money was
refunded. The ledger retained 15 reservations totaling $27.14. This confirmed
termination and financial reconciliation does not turn the failed timing or
preview-decode receipt into a pass. The warm services were redeployed afterward;
no further paid browser test was launched.

The third warm startup check on 12 November 2026 opened two fresh public visitors
simultaneously. Director On appeared at **4.463 and 4.132 seconds from navigation**,
or 2.340 and 2.388 seconds after their first factual native playback reports.
Both used the frozen real model, had overlapping active inference, captions off,
and no explicit Auto On request. First complete seven-source score cohorts arrived
at 13.466 and 11.721 seconds from navigation; readiness within five seconds does
not mean a complete scored observation window is available within five seconds.
The check recorded five and seven advancing complete cohorts in clock epoch zero,
with advancing native playback and zero page, socket, server-frame or reconnect
errors. Both UI stop acknowledgements and component-permit closure were verified.

The complete browser receipt still failed its separate preview-decode condition:
both browsers had decoded six of seven previews when its short extra wait ended
at about 24 seconds. The seventh had no media error but had not produced a frame.
The receipt remains failed; the measured five-second Director readiness result
is narrower than a successful complete browser check. Earlier warm attempts also
remain recorded as failures; this is a two-visitor warm measurement, not a cold
start or arbitrary-concurrency guarantee. Eight workers support two warm starts;
additional demand or provider replacement can require cold capacity.

The admission change passed 1,081 tests with one optional Torch skip, plus
installed-wheel verification. The unchanged frontend had passed 240 tests and
its production build. DynamoDB migration preserved all 11 existing reservations
($16.74), both unresolved historical owners and the peer salt. After the startup
check, the permanent ledger held 13 reservations ($21.94); both new component
permits were closed, and total counters matched every historical amount. The old
SQLite volume and exact pre-migration backup were retained. The new scoped
backend OIDC role can read recordings and access the admission table; the prior
S3-only role is unchanged.

ASGI readers now continue processing factual playback, Off and disconnect while
durable admission runs. Pending requests have per-component intent/connection
fences, and stale grants are closed before any provider starts. The deployed
DynamoDB store avoids volume commits on admission: measured creation writes took
0.058–0.144 seconds and Director reservation writes 0.093–0.110 seconds.

A separate unpaid native-media diagnostic then observed six HTTP/1.1 206 video
streams occupying six connections for about 119 seconds, while the seventh
request remained pending with zero bytes. Auto was cancelled before the first
factual playback report, no paid feature became active, and its visitor had no
financial permit in the audited ledger. This confirmed transport connection
starvation rather than a short decode polling window. The browser-origin split
described above passed 1,103 backend tests, one optional Torch skip, and package
verification. Read-only AWS preflight checked all seven objects through both
endpoints: matching ETag/length, signed HEAD, one-byte HTTP 206 and production
CORS. The prior failed browser receipts remain unchanged.

The historical verification notes below describe their stated earlier deployments.

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

The independent twenty-minute policy was deployed on 12 November 2026 after
1,035 backend tests, 239 frontend tests, production builds and installed-wheel
checks passed. Provider-free tests cover parallel visitors, independent controls,
deadline expiry, immediate restart after confirmed cleanup, ten-start rolling
limits, budget races and stale-socket ownership during delayed admission. The
public rollout check confirmed two distinct zero-time visitors, default-on
Director intent, captions initially off and both 1200-second limits. It canceled
startup before sending playback, made no paid enable request and closed both
sockets. HTTP health, standings, news and production-origin CORS passed. A
committed-ledger audit still showed four closed reservations totaling $2.60,
leaving the new $10 available; no new GPU input ran for this verification.

The new controls and twenty-minute expiry have not been rechecked with paid AI.
The earlier live evidence below used the former two-minute policy and retains
its sustained seven-source scoring limitation.

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
The fix passed all 233 frontend tests and the production build, then an unpaid
check against the deployed public site. That browser decoded and manually
switched all seven recordings with preserved player nodes and zero measured
switch-clock deviation, resumed the same session after reload, and reported
0.885 seconds of native clock advancement after an injected buffered network
stall. Auto and captions remained off, no paid enable request was sent, the
browser closed, and no page, media, socket or CORS errors occurred. This fix has
not been rechecked with paid AI, and its role in the live score shortfall is
unproven.

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
