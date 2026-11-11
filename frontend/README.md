# Touchline frontend

The viewer uses React and Create React App. Run these commands from the repository root:

```powershell
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend start
```

The default development URL is `http://localhost:3000`. Follow the root README to launch the backend and configure `REACT_APP_WS_URL` and `REACT_APP_API_URL`. Frontend environment variables are public; keep provider credentials on the backend.

Run the existing tests and make a production build with:

```powershell
npm.cmd --prefix frontend test -- --watchAll=false --runInBand
npm.cmd --prefix frontend run build
```

The build output is `frontend/build/`. For Vercel, select `frontend` as the root directory, `npm run build` as the build command, and `build` as the output directory. The build requires the full repository checkout so its preparation script can read the canonical fixture catalogue.

## Source layout

- `src/app/`: application shell, theme, navigation and the ordered stylesheet entry point.
- `src/features/watch/`: the permanently mounted Watch view, with `session/` coordination, `playback/`, `audio/`, `director/` and `sources/` modules.
- `src/features/captions/`: live and stored caption handling.
- `src/features/tables/` and `src/features/news/`: the supporting pages.
- `src/shared/`: backend connections and reusable interface elements.
- `src/__tests__/`: integration tests; focused module tests sit beside their modules.

Watch navigation hides the existing view. It does not conditionally mount players.
`useWatchReplay` coordinates session messages; `usePlayerHandoff` owns ready-player
promotion and acknowledgement; `useDirectorControls` owns acknowledged mode changes.
The older test-only players are explicitly retained under `watch/compatibility/`.

Feature styles are colocated with their code. `src/app/styles.css` imports their
contiguous sections in the original global cascade order. Cross-feature responsive
and accessibility rules remain in `app/`; preserve this order when changing styles.

`backend/src/touchline/data/matches.json` is the canonical fixture catalogue. The `prestart`, `prebuild` and `pretest` scripts copy its exact bytes into the ignored `src/data/matches.json` for CRA to import. Edit the canonical file; do not maintain a frontend copy. The sync script resolves paths from its own location, so it also works when invoked from another directory.

The seven player elements remain mounted when switching matches. Preserve their identities, playback acknowledgements and audio ownership when changing this area.

Private playback is the default: the seven initial cards have no media URLs until
the backend registers signed sources. `REACT_APP_SOURCE_ACCESS=public` explicitly
enables the older public demo fallback for authorized local fixtures; the test
setup opts into that mode, while dedicated tests exercise the private default.
Permanent AWS credentials and signing remain on the backend.

Signed registrations include stable source identity and `url_expires_at` in Unix
seconds. A rotated signature does not reload a healthy player or reset captions.
Each player retains its active URL until shortly before expiry, then restores its
source position and playback intent on the same element using the latest signed
URL. Errors also recover with the newest authorization. The backend must publish
fresh registrations before current authorizations expire; the browser does not
sign URLs. Verified migrated fixture metadata requires the backend's
`fixture_verified` attestation, matching source identity and unchanged canonical
fixture fields and score intervals.

To build the optional frontend container from the repository root:

```powershell
docker build -f frontend/Dockerfile -t touchline-frontend .
```
