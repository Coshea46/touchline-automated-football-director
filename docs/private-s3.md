# Private recording access

Touchline now defaults to signed reads from the owner's private bucket. This is
application code and configuration. A bounded Modal check on 12 November 2026
verified the OIDC role exchange, all seven fixture bindings, independently signed
HEAD requests, one-byte HTTP 206 GET ranges and CORS for both
`http://localhost:3030` and `https://touchline-football-director.vercel.app`.
A separate public browser check decoded all seven recordings, observed advancing
native playback, preserved the seven video nodes through manual switches and
resumed a nonzero replay after same-tab reload. The maximum switch clock deviation
was 0.002 seconds. A separate bounded T4 operator check verified complete real-model
inference cohorts across all seven sources. Full-file checksums and the complete
public browser Auto path remain unverified. See
[hosted verification status](modal-backend.md#frontend-connection-and-verification).

The T4/L4 compute capacity fallback retains the same private source bindings and
signed-read protocol. It does not change the recordings, frozen model or raw
feature/clock contracts. L4 verification is pending; the T4 result above does not
establish L4 readiness or public Auto startup latency.

## Configured resources

| Setting | Value |
|---|---|
| Bucket | `touchline-videos-709860105019-eu-west-2-an` |
| AWS region | `eu-west-2` |
| Reader role | `arn:aws:iam::709860105019:role/TouchlineVideoReader` |
| Modal workspace | `ac-NmeTt6xv3IOOQ9wmPVZHBM` |
| Access mode | `MATCHPULSE_SOURCE_ACCESS=presigned` |
| Requested URL lifetime | `MATCHPULSE_SOURCE_URL_TTL=3600` seconds |

The nonsecret defaults live in
[`config/video_sources.py`](../backend/src/touchline/config/video_sources.py).
The recordings retain their existing `match-1.mp4` through `match-7.mp4` keys,
match IDs and playback offsets. The canonical catalog is
[`data/matches.json`](../backend/src/touchline/data/matches.json).

The reported IAM trust scope covers the workspace subject pattern
`modal:workspace_id:ac-NmeTt6xv3IOOQ9wmPVZHBM:*`. Once production environment and
application names are fixed, narrow that scope to the intended environment/app.
AWS verifies the Modal token's signature, audience and trusted subject; the
application does not accept locally decoded claims as authorization.

## Credential and URL lifetime

`touchline.storage.S3Access` initializes AWS access only when requested. In Modal,
the injected `MODAL_IDENTITY_TOKEN` and configured `AWS_ROLE_ARN` select STS
`AssumeRoleWithWebIdentity`. Botocore refreshes the returned temporary credentials;
each exchange reads the current token from the live environment/provider.
Credentials are not copied into global environment variables or the frontend.

Local runs use the normal AWS credential chain, including an existing
`AWS_PROFILE`. Setting a role ARN locally does not impersonate a Modal function or
trigger the Modal exchange without its identity token. Use an authenticated local
profile; do not paste a Modal token into local configuration.

GET and HEAD have separate signatures. The browser and media readers receive the
GET URL; server-side identity checks use a separately signed HEAD URL for the same
object. A GET signature cannot authorize a HEAD request. Range headers are not
fixed into the download signature, allowing subsequent browser/media range reads.

Each signature uses one immutable credential generation. Its advertised deadline
is capped by the actual credential expiration with a 30-second margin, even when
the requested URL lifetime is longer. Local temporary credentials with no known
expiration fail with `aws_credential_expiry_unknown` instead of advertising an
unjustified deadline. Prefer a refreshable profile/provider or normal credentials
that report their expiration.

Docker does not inherit the host's AWS profile files. For a local container using
a profile, mount its AWS configuration and credentials read-only and supply the
profile name inside the container, or use the container platform's credential
provider. The role ARN alone does not provide local container credentials.

The registry renews source authorization before its deadline and distributes the
new URLs without changing match IDs, offsets, the replay epoch or model permits.
Refresh work runs separately from worker heartbeats, with bounded retry backoff.
The frontend waits for registered backend sources; it does not fall back to an
anonymous URL for the private bucket. Worker recovery uses the latest authorized
source URL. Authorization failures remain visible and do not silently select a
public source.

Native browser players keep their current decoder after a signature update and
adopt the latest URL before expiry on the same video element. Playback waits for
the restored source position before reporting readiness. The focused caption
decoder also renews its URL at the last submitted audio boundary while retaining
the existing Scribe connection, transcript anchor and session allowance.

## What the copied-recording binding proves

The owner confirmed that the seven destination objects are unchanged copies of
the original recordings. That statement is recorded as
`operator_confirmed_unmodified_copy` in the catalog's `source_bindings`.

At resolution, authenticated S3 HEAD results must match each original recording's
content length. The application reads the destination's actual ETag and constructs
its destination identity from that ETag, byte length and object locator. The old
inspected identity remains `origin_source_identity`; it is not reused as an
invented destination identity. An identity change within the session is rejected.

This combines the owner's copy attestation with authenticated metadata checks.
It is **not an independent full-file checksum comparison**. Matching byte lengths
alone do not prove equal contents, and multipart ETags are not whole-file hashes.
Verification receipts explicitly keep `full_file_checksum_verified=false`.

## Browser CORS

The configured CORS rule includes these exact origins:

- `http://localhost:3000`
- `http://127.0.0.1:3000`
- `http://localhost:3030`
- `http://127.0.0.1:3030`
- `https://touchline-football-director.vercel.app`

The settings permit `GET` and `HEAD`, allow request headers `*`, expose
`ETag`, `Content-Length`, `Content-Range` and `Accept-Ranges`, and use a preflight
cache age of 3600 seconds. The 12 November 2026 Modal OIDC probe passed CORS checks
for the production origin across all seven recordings. The full rule is recorded
in [`deployment/s3-cors.json`](deployment/s3-cors.json). CORS controls browser
access to responses; it does not grant S3 authorization or make the bucket public.

## Explicit read checks

Run commands from the repository root after [backend installation](../backend/README.md).
The local check uses existing AWS credentials and the source defaults above. Set
your authenticated profile in the invoking shell, if needed:

```powershell
$env:AWS_PROFILE = 'your-local-profile'
& backend/.venv/Scripts/python.exe -B -m touchline.tools.storage.verify_sources --output backend/artifacts/storage/private-sources-local.json
```

The utility loads the root `.env` without replacing shell settings, and checks all
seven objects through authenticated metadata, signed HEAD requests and one-byte
GET range requests. A passing report requires the expected fixture binding,
matching HEAD metadata, HTTP 206 with the correct content range, and CORS responses
allowing `http://localhost:3030`. Use `--origin` with the local command to check a
different frontend origin. It does not download whole matches or run feature extraction.

To check the Modal identity exchange, the following opt-in command starts one
bounded CPU-only function. It requires an authenticated Modal client and the
configured IAM trust; it does not require deploying the application:

```powershell
& backend/.venv/Scripts/python.exe -m modal run -m touchline.tools.storage.modal_probe --allow-read
```

The Modal check performs the same small S3 reads. It starts no GPU inference or
transcription. Add `--origin https://touchline-football-director.vercel.app` to
check the production origin. Both the local-origin and production-origin Modal
checks passed on 12 November 2026. These commands check metadata/ranges/CORS;
the separate public browser playback and bounded operator inference checks also
passed. The complete public browser Auto path remains unverified. Normal unit
tests use fake STS/S3 adapters and make no provider requests.

Historical model-release and legacy equivalence commands retain their original
public recording fixture. Use the storage commands above to check this private
bucket; prior model benchmark results do not verify the migrated objects.

Reports contain keys, byte counts, identity digests and result codes, without
signed URLs or credential tokens. Keep bearer URLs and tokens out of logs,
screenshots and pasted diagnostics; share the sanitized JSON receipt instead.

## Application access remains separate

Private S3 objects require valid authorization, but a reachable backend can still
distribute signed URLs to its viewers. Bucket privacy does not implement viewer
authentication or decide who may control a replay. The public deployment gives
each visitor an independent replay session, with all seven recordings starting
at zero and a per-tab resume capability. A visitor's playback, selection and AI
controls belong to that session. The public link remains subject to session,
rate and paid-work admission limits; it does not require an account sign-in.
See [the hosted backend](modal-backend.md) for those controls and the separate
durable AI allowance. The local launcher retains its shared-session mode.
