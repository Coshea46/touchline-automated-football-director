import { ChevronDown } from 'lucide-react';
import { DIRECTOR_MODES } from './director';

const MODE_HELP = { relaxed: 'Fewer automatic switches.', balanced: 'A balanced switching pace.', all_action: 'More frequent automatic switches.' };

export default function DirectorPanel({ director, mode, pendingMode, notice, onMode, enabled, available = true, modelVersion, held, canToggle, requested,
  holdUntil, now, monitoring, matchCount, monitoredCount, starting, connected, connection, canControl, scoreFresh,
  inferenceLabel, autoLabel, autoTitle, onToggle, canClaimControl = false, controlPending = false,
  controlNotice, onClaimControl }) {
  const cooldown = Math.max(0, Math.ceil((director?.auto_cooldown_until || 0) - now));
  const connectionNotice = connection === 'connecting' ? 'Connecting to Auto Director…'
    : !connected ? 'Director offline. You can still choose a match.' : !canControl
      ? `Viewing only. Another tab controls this replay.${canClaimControl ? '' : ' Restart the backend to enable control handoff.'}` : '';
  return (
    <aside className="director-panel" aria-label="Auto Director settings">
      <div className="director-controls"><div className="director-heading"><h2>Auto Director</h2>
        <button className="director-toggle" aria-label={autoLabel} aria-pressed={enabled} aria-busy={(starting && requested) || undefined}
          title={autoTitle} disabled={canToggle === undefined ? !available || canControl === false : !canToggle} onClick={onToggle}>
          <span>{starting && requested ? 'Starting…' : enabled ? 'On' : 'Off'}</span><span className={`toggle-track ${enabled ? 'is-on' : ''}`} aria-hidden="true"><i /></span>
        </button></div>
      <div className="mode-field">
        <label htmlFor="director-mode">Director mode</label>
        <div className="select-wrap">
          <select id="director-mode" value={pendingMode || mode} disabled={!connected || !director || !canControl}
            aria-describedby="director-mode-description" onChange={(event) => onMode(event.target.value)}>
            {Object.entries(DIRECTOR_MODES).map(([id, item]) => <option key={id} value={id}>{item.label}</option>)}
          </select>
          <ChevronDown size={17} aria-hidden="true" />
        </div>
      </div>
      <p className="director-state" role="status" title={monitoring && modelVersion ? `Active model: ${modelVersion}` : undefined}>{starting ? `Preparing ${matchCount} matches`
        : monitoring ? `Monitoring ${monitoredCount === matchCount ? matchCount : `${monitoredCount} of ${matchCount}`} ${matchCount === 1 ? 'match' : 'matches'}`
          : connected ? inferenceLabel || 'Monitoring off' : ''}</p>
      {held && <span className="director-hold">Your selection · {Math.max(0, Math.ceil(holdUntil / 1000 - now))}s remaining</span>}
      {!held && enabled && cooldown > 0 && <span className="director-hold">Next switch in {cooldown}s or later</span>}
      </div>
      <div className="director-notes"><p id="director-mode-description">{MODE_HELP[pendingMode || mode]}</p>
      {director?.config && <div className="director-rules" aria-label="Applied director settings">
        <span>{director.config.cooldown_seconds}s between switches</span>
        <span>{director.config.confirmations_required} score {director.config.confirmations_required === 1 ? 'check' : 'checks'}</span>
      </div>}
        {monitoring && !scoreFresh && <span>Waiting for fresh scores</span>}
        {monitoring && inferenceLabel === 'Mock scores' && <span>Mock scores</span>}
        {available && !enabled && <span>You choose the match</span>}
      </div>
      {connectionNotice && <p className="director-notice" role="status">{connectionNotice}</p>}
      {connected && !canControl && canClaimControl && <button type="button" className="theme-toggle"
        aria-label="Use this tab" aria-busy={controlPending || undefined} disabled={controlPending} onClick={onClaimControl}>
        {controlPending ? 'Requesting control…' : 'Use this tab'}
      </button>}
      {controlNotice && <p className="mode-notice" role="status">{controlNotice}</p>}
      {pendingMode && <p className="mode-notice" role="status">Applying {DIRECTOR_MODES[pendingMode].label}…</p>}
      {notice && <p className="mode-notice" role="status">{notice}</p>}
    </aside>
  );
}
