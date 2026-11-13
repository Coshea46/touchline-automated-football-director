# On-demand startup tutorial

Production opens a 70.4-second narrated Synthesia tutorial, then “Setting things up”,
then fades into the app only after the server reports the Director available.
Fresh hosted sessions default to All Action; an explicitly saved viewer mode
still overrides the default when the client joins.
Skip advances to setup, not past readiness. The tutorial tries audible playback
first. If the browser blocks it, playback continues muted with a prominent
“Turn sound on” button. If muted playback is also blocked, “Play tutorial with
sound” starts it through a user gesture. Native volume controls remain available.
The generated illustrative interface is labeled as such. The static MP4 is
served from Vercel; Synthesia is not called for each visitor.

Video source: [Synthesia editor](https://app.synthesia.io/syren/video-edit/a9dde434-0998-4ac3-94b6-0b9ea47d6d17).
The exported asset is `frontend/public/media/touchline-tutorial.mp4`.
The website export preserves the 1920×1080 video stream and normalizes the AAC
narration to -16 LUFS (measured -16.02 LUFS, -1.45 dB true peak). The versioned
media URL prevents reuse of the earlier tutorial from the browser cache.

The tutorial explains the Director as the automatic match selector, the custom
model built by Conor O’Shea, parallel analysis of all seven matches, and the
separate internal scoring system that chooses the next match. It describes the
current implementation: a five-second observation window every **two** seconds
(`streaming/runner.py` advances 30 frames at 15 fps). All Action is the default,
manual selection remains available, and captions have an independent switch.
The brand's spoken pronunciation is “touch line”; its displayed spelling stays
Touchline. The tutorial contains no demo disclosure or zero-clock explanation.
The page immediately below the video says that the matches are recordings used
to demonstrate Touchline and are not live. Readiness and playback still use the
existing server contract; editing the tutorial does not change model cadence.

Each onboarding starts a fresh visitor capability. The focused recording decodes
at zero while paused. Only after reporting that actual paused clock does the
client send one `setup` request. The existing admission policy charges one
Director lease; repeated setup requests cannot renew it. Startup holds the
paused-worker idle timer for at most 120 seconds, without fabricating playback,
scores or elapsed media. The ordinary 20-minute paid deadline remains in force.
Cancel and the two-minute UI deadline close the WebSocket and trigger owned
cleanup. Retry explicitly creates a fresh session and is subject to admission.

The setup hold also travels in the worker control packet's `startup_waiting`
flag after the whole group reports ready. Otherwise the worker's normal
30-second pause guard could stop it before the 70.4-second tutorial finishes.
The first accepted playing report consumes this hold; a later pause cannot
restore it. The existing two-minute bound, heartbeat expiry and owned shutdown
still apply. Worker terminal diagnostics use bounded labels in backend logs;
the UI distinguishes a stopped session from a connection failure.

Both CPU and GPU deployments use `min_containers=0`, `buffer_containers=0` and
a 30-second scale-down window. An open visitor connection still consumes CPU;
active setup/Director sessions consume GPU. Storage and request charges remain.
No periodic keepalive creates workers. The current reservation ceilings are
$55.42 retained total and $22.80 per UTC day, including the owner's latest
approved $5 increase. Earlier reservations remain counted. These application
limits are not provider invoice caps. A Director start reserves $2.60
conservatively.

Cold GPU startup can still fail: one public attempt on 13 November had three
worker connections time out at 110 seconds. GPU container placement now uses
Modal's default global pool instead of requiring Europe, retaining L4/T4 fallback
and zero warm workers. This removes a placement restriction; another successful
paid cold-start test has not been run after this change. No allowance was raised
for this fix. The UI preserves the startup failure, spending-limit or explicit
cancellation message after closing its connection; the timeout cannot replace
that reason with a generic cancellation message.

`REACT_APP_STARTUP_TUTORIAL=false` disables onboarding explicitly. Production
enables it by default; local/test environments opt in with `true`.

Automatic failures cancel the backend connection immediately but do not interrupt
the tutorial. The availability notice appears alongside the same playing video.
After the video ends or is skipped, the preserved failure and manual-viewing
option are shown. Exhausted funding has no futile Retry button. Explicit user
Cancel still closes the tutorial immediately. Neither automatic failure nor
manual entry reconnects or makes another paid request. The tutorial's timer
cannot replace the recorded failure reason or steal focus on a hidden transition.

## Verification

The tutorial worker-hold fix passed 466 tests across replay, streaming and hosted
backend suites, the installed-wheel checks, the startup frontend tests and the
production build. Regression coverage holds a ready group at zero throughout
the tutorial, releases it on real playback, retains later pause shutdown, and
checks hold/heartbeat expiry. The worker harness also waits 70.4 seconds before
processing the first permitted five-second observation without restarting.
A bounded real-model operator check then became ready in 14.571 seconds, held
that ready group paused at zero for another 57.523 seconds, and received model
scores from all seven sources after releasing playback. Owned worker shutdown
and source cleanup were confirmed. This used a controlled operator permission
clock, not browser playback, and a 120-second compute bound with a $0.75 durable
reservation. Its earlier harness configuration failure also retains its $0.75
reservation; no historic charges or uncertain permits were refunded.

The tutorial revision passed 253 frontend tests and a production build. A
headless Edge check with audible autoplay blocked confirmed muted playback,
one-click sound, the disclosure outside the video, mobile layout, uninterrupted
playback on resize, and skip/failure handling. Its backend sockets were
intercepted; it sent no paid startup requests and closed the browser afterward.
Audio-track, duration and loudness checks passed on the actual shipped MP4.
The “touch line” pronunciation alias is applied in Synthesia; automated audio
level checks cannot confirm subjective pronunciation.

1,104 backend tests passed (one optional Torch skip); 244 frontend tests passed,
with production build and installed-wheel checks. A real public cold-start check
on 13 November reported Director readiness at 22.059 seconds and app entry at
46.936 seconds after navigation. The 44.7-second tutorial played, all 168 actual
playback reports while the overlay existed were paused at zero, exactly one
setup request was sent, and native playback advanced after entry. UI Director
Off reached the stopped lifecycle; the browser closed with no page/server errors.

Earlier receipts are preserved: the initial recorder incorrectly counted the
first post-overlay playback report as early due to a Node polling race. The next
check correctly stayed paused but was denied by the conservative spending cap.
The cap was corrected from an authoritative read of the retained ledger, not by
refunding reservations. The final recorder samples overlay presence synchronously
at each native WebSocket send without changing its payload, and passed.
After the final check, Modal reported zero CPU/GPU runners, zero running inputs
and zero backlog. Both deployed services remained available on demand.

The later tutorial-denial fix passed 250 frontend tests and its production build.
A browser check against the production UI intercepted a synthetic budget denial:
the tutorial remained visible, the backend never connected, there was no retry
loop, and manual entry succeeded. Under the owner's separately authorized $10
testing allowance, one real cold-start check then passed with readiness at
23.573 seconds and app entry at 46.162 seconds. All 170 reports while the overlay
existed were paused at zero; one setup request was sent, native playback advanced
after entry, stopped lifecycle was confirmed, and the browser closed without
page/server errors. Prior receipts remain unchanged.
