import { registeredFeeds } from '../sources/feeds';

export const PROTOCOL_VERSION = 1;
export const MAX_RECOMMENDATION_AGE_SECONDS = 3;

const validSequence = (value) => Number.isSafeInteger(value) && value >= 0;

export const isUnavailableFallback = (message, playback) => playback.available === false
  && message.selection_kind === 'fallback' && message.selection_reason === 'unavailable_fallback';

export function readSession(message, previous) {
  if (message.type !== 'session' || message.protocol_version !== PROTOCOL_VERSION
    || typeof message.session_id !== 'string' || !message.session_id
    || typeof message.model_version !== 'string' || !message.model_version
    || ['auto_available', 'auto_requested', 'auto_can_start', 'auto_starting', 'can_claim_control'].some((key) =>
      message[key] !== undefined && typeof message[key] !== 'boolean')
    || !['real', 'features_mock', 'disabled'].includes(message.mode) || !validSequence(message.sequence)
    || !Number.isFinite(message.server_time)
    || !Number.isFinite(message.playback?.position) || message.playback.position < 0
    || (message.playback.clock_epoch !== undefined && !validSequence(message.playback.clock_epoch))
    || (previous?.session_id === message.session_id && message.sequence <= previous.sequence)) return null;
  const feeds = registeredFeeds(message.matches);
  if (!feeds.length || feeds.length !== message.matches.length) return null;
  return { ...message, feeds };
}

// A single latest message is considered immediately. Held/disabled decisions
// are discarded, never queued for playback after the viewer has moved on.
export function validRecommendation(message, session, playback, now = Date.now() / 1000) {
  if (!session || session.auto_available === false || !['real', 'features_mock'].includes(session.mode)
    || message.type !== 'recommendation' || message.protocol_version !== PROTOCOL_VERSION
    || message.session_id !== session.session_id || message.model_version !== session.model_version
    || !validSequence(message.sequence) || message.sequence <= session.sequence
    || message.clock_epoch !== session.playback.clock_epoch
    || message.score_kind !== (session.mode === 'real' ? 'model' : 'mock')) return false;
  const target = session.feeds.find((feed) => feed.cameraId === message.selected_match_id);
  if (!target || !target.healthy
    || ((playback.stalled || playback.available === false) && !isUnavailableFallback(message, playback))) return false;
  if (!message.scores || typeof message.scores !== 'object' || Array.isArray(message.scores)
    || !Object.hasOwn(message.scores, message.selected_match_id)
    || !Object.entries(message.scores).every(([id, score]) => session.feeds.some((feed) => feed.cameraId === id)
      && Number.isFinite(score) && score >= 0 && score <= 1)) return false;
  const numbers = [message.playback_time, message.observation_end, message.emitted_at, message.valid_until];
  if (!numbers.every(Number.isFinite) || message.valid_until <= now || message.emitted_at > now + 1
    || message.valid_until < message.emitted_at || now - message.emitted_at > MAX_RECOMMENDATION_AGE_SECONDS) return false;
  // Permit tiny timestamp rounding only. No observation beyond the actual
  // decoded viewer position is ever accepted, even if the wall clock is ahead.
  if (message.playback_time < 0 || message.observation_end < 0 || message.observation_end > playback.time + 0.05
    || message.playback_time > playback.time + 0.25
    || playback.time - message.playback_time > MAX_RECOMMENDATION_AGE_SECONDS
    || playback.time - message.observation_end > MAX_RECOMMENDATION_AGE_SECONDS) return false;
  return true;
}
