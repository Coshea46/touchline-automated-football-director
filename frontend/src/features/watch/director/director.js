export const DIRECTOR_MODES = {
  relaxed: { label: 'Relaxed', description: 'Fewer automatic switches.' },
  balanced: { label: 'Balanced', description: 'Standard switching pace.' },
  all_action: { label: 'All Action', description: 'More frequent automatic switches.' },
};
export const MODE_STORAGE_KEY = 'matchpulse-director-mode';

export function preferredMode(fallback = 'balanced') {
  try {
    const value = localStorage.getItem(MODE_STORAGE_KEY);
    return Object.hasOwn(DIRECTOR_MODES, value) ? value : fallback;
  } catch { return fallback; }
}

export function readDirector(value, feeds) {
  if (!value || !Object.hasOwn(DIRECTOR_MODES, value.mode)) return null;
  const ids = new Set(feeds.map((feed) => feed.cameraId));
  const config = value.config;
  const validConfig = config && Number.isFinite(config.cooldown_seconds) && config.cooldown_seconds >= 0
    && Number.isFinite(config.staying_bonus) && config.staying_bonus >= 0 && config.staying_bonus <= 1
    && Number.isInteger(config.confirmations_required) && config.confirmations_required > 0;
  const scores = Object.fromEntries(Object.entries(value.latest_scores || {}).filter(([id, score]) =>
    ids.has(id) && Number.isFinite(score) && score >= 0 && score <= 1));
  const history = (Array.isArray(value.history) ? value.history.slice(-100) : []).filter((event) =>
    event && (typeof event.id === 'string' || Number.isSafeInteger(event.id)) && ids.has(event.to_match_id)
    && (event.from_match_id === null || ids.has(event.from_match_id))
    && ['automatic', 'manual', 'playback', 'fallback'].includes(event.kind)
    && Number.isFinite(event.selected_at) && Number.isFinite(event.playback_time));
  return { ...value, config: validConfig ? config : null, latest_scores: scores, history };
}

export function ageLabel(timestamp, now) {
  if (!Number.isFinite(timestamp)) return 'Not yet';
  const seconds = Math.max(0, Math.floor(now - timestamp));
  return seconds < 2 ? 'Just now' : seconds < 60 ? `${seconds}s ago` : `${Math.floor(seconds / 60)}m ago`;
}

export function replayTime(value) {
  const seconds = Math.max(0, Math.floor(Number.isFinite(value) ? value : 0));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}
