import { Pause, Play } from 'lucide-react';
import PersistentMatchPlayer from './playback/PersistentMatchPlayer';
import ReplayScore from './playback/ReplayScore';
import DirectorPanel from './director/DirectorPanel';
import CommentaryCaptions from '../captions/CommentaryCaptions';

export default function WatchView({ active, replay }) {
  const { feeds, focused, focusedRef, headingRef, registerPlayer, clock, seekTo, reportPlayback, mainVideo,
    audio, audioEnabled, claimPending, claimControl, session, sessionState, director, pendingMode, modeNotice,
    requestMode, controlNotice, holdUntil, connection, captions, autoSwitch, previewsPaused, setPreviewsPaused,
    pick, unavailable, finishRecommendation, pendingAck, toggleAuto } = replay;
  const { autoAvailable, autoStarting, autoToggleable, held, autoLabel, autoTitle, inferenceBuffering,
    inferenceLabel, now, monitoring, mode, scoreFresh, scores } = replay.view;
  return (
        <section id="watch" className="player-layout" hidden={!active} aria-label="Watch matches">
          <div className="player-heading"><div className="fixture-heading">
            <h1 ref={headingRef} tabIndex={-1} style={{ viewTransitionName: 'match-heading' }}>{focused.label}</h1><ReplayScore feed={focused} mediaRef={mainVideo} />
          </div>
            {(audio.status === 'other-tab' || audio.pending) && <button type="button" className="theme-toggle audio-ownership"
              title="Move Touchline audio to this tab"
              onClick={() => {
                if (session.current?.controls_enabled === false) claimControl();
                else void audio.requestAudio();
              }} disabled={audio.pending || claimPending || (sessionState?.controls_enabled === false && sessionState?.can_claim_control !== true)}>
              {audio.pending ? 'Switching audio…' : 'Play audio here'}
            </button>}
            {audio.status === 'unavailable' && <span className="audio-ownership" role="status">Audio needs a browser with exclusive playback support.</span>}
          </div>
          <div className="watch-stage">
            <div className="filmstrip player-pool" role="region" aria-label="Other matches">
              {feeds.map((feed) => {
                const selected = feed.id === focused.id;
                const index = feeds.filter((item) => item.id !== focused.id).findIndex((item) => item.id === feed.id);
                return <PersistentMatchPlayer key={feed.cameraId} feed={feed} focused={selected} focusedRef={focusedRef}
                  registerPlayer={registerPlayer}
                  clock={clock} seekTo={seekTo} onPlayback={reportPlayback} mediaRef={mainVideo}
                  inferenceBuffering={inferenceBuffering} audioAllowed={audio.owned && audioEnabled}
                  previewsPaused={previewsPaused} watching={active} onClick={() => pick(feed.id)}
                  onAvailability={(matchId, available) => {
                    if (available) { unavailable.current.delete(matchId); finishRecommendation(); }
                    else { unavailable.current.add(matchId); if (selected) pendingAck.current = null; }
                  }}
                  score={scores[feed.cameraId]} scoreKind={sessionState?.mode === 'features_mock' ? 'mock' : 'model'} monitoring={monitoring}
                  style={selected ? undefined : { '--preview-row': Math.floor(index / 3), '--preview-column': index % 3,
                    '--preview-row-mobile': Math.floor(index / 2), '--preview-column-mobile': index % 2 }} />;
              })}
            </div>
            <div className="watch-sidebar">
              <DirectorPanel director={director} mode={mode} pendingMode={pendingMode} notice={modeNotice} onMode={requestMode}
              enabled={autoAvailable && autoSwitch} available={autoAvailable} modelVersion={sessionState?.model_version}
              canToggle={autoToggleable} requested={autoSwitch}
              canClaimControl={sessionState?.can_claim_control === true} controlPending={claimPending}
              controlNotice={controlNotice} onClaimControl={claimControl}
              held={held} holdUntil={holdUntil} now={now} monitoring={monitoring} matchCount={feeds.length}
              monitoredCount={feeds.filter((feed) => feed.healthy !== false).length} starting={inferenceBuffering || autoStarting}
              connected={connection === 'connected'} connection={connection} canControl={sessionState?.controls_enabled !== false}
              scoreFresh={scoreFresh} inferenceLabel={inferenceLabel} autoLabel={autoLabel} autoTitle={autoTitle} onToggle={toggleAuto} />
              <CommentaryCaptions feed={focused} mediaRef={mainVideo} live={captions.value} enabled={captions.enabled}
                onToggle={captions.control} connected={connection === 'connected'}
                canControl={sessionState?.controls_enabled !== false} />
            </div>
        <div className="strip-toolbar"><h2>Other matches <span>{Math.max(0, feeds.length - 1)}</span></h2>
          <button className="icon-button" aria-label={previewsPaused ? 'Resume previews' : 'Pause previews'}
            title={previewsPaused ? 'Resume previews' : 'Pause previews'} aria-pressed={previewsPaused}
            onClick={() => setPreviewsPaused(!previewsPaused)}>
            {previewsPaused ? <Play size={18} aria-hidden="true" /> : <Pause size={18} aria-hidden="true" />}
          </button>
        </div>
        <p className="selection-help">{feeds.length === 1 ? 'No other matches in this replay.' : 'Choose a match to keep it for 30 seconds.'}</p>
          </div>
        <p className="watch-disclosure">Replay footage. Scores are relative and experimental, not goal probabilities.</p>
        </section>
  );
}
