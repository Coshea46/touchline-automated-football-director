# On-demand startup tutorial

Production opens a 44.7-second Synthesia tutorial, then “Setting things up”,
then fades into the app only after the server reports the Director available.
Skip advances to setup, not past readiness. Narration can be enabled with the
native video controls; muted autoplay supports browser playback policies.
The generated illustrative interface is labeled as such. The static MP4 is
served from Vercel; Synthesia is not called for each visitor.

Video source: [Synthesia editor](https://app.synthesia.io/syren/video-edit/a9dde434-0998-4ac3-94b6-0b9ea47d6d17).
The exported asset is `frontend/public/media/touchline-tutorial.mp4`.

Each onboarding starts a fresh visitor capability. The focused recording decodes
at zero while paused. Only after reporting that actual paused clock does the
client send one `setup` request. The existing admission policy charges one
Director lease; repeated setup requests cannot renew it. Startup holds the
paused-worker idle timer for at most 120 seconds, without fabricating playback,
scores or elapsed media. The ordinary 20-minute paid deadline remains in force.
Cancel and the two-minute UI deadline close the WebSocket and trigger owned
cleanup. Retry explicitly creates a fresh session and is subject to admission.

Both CPU and GPU deployments use `min_containers=0`, `buffer_containers=0` and
a 30-second scale-down window. An open visitor connection still consumes CPU;
active setup/Director sessions consume GPU. Storage and request charges remain.
No periodic keepalive creates workers. The operator caps new public reservations
to $10 beyond retained spending, with a $10 UTC-day cap. This application limit
is not a provider invoice cap. A Director start reserves $2.60 conservatively.

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
