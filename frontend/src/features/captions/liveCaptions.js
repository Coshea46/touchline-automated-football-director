export const LIVE_CAPTION_MODEL = 'scribe_v2_realtime';
const STATES = new Set(['connecting', 'listening', 'partial', 'committed', 'paused', 'unavailable', 'stopped', 'error']);

export const captionSessionEnded = (value) => ['session_closed', 'all_sources_ended', 'session_ended', 'source_ended'].includes(value?.reason);

export function liveCaptionStatus(value, connected = true, canControl = true) {
  const reason = value?.reason || value?.status;
  if (['auth_error', 'provider_permission_speech_to_text_missing'].includes(reason)) return 'ElevenLabs could not authorize live captions. Ask the demo operator to check Speech to Text access.';
  if (reason === 'feature_disabled') return 'Live captions are disabled for this demo.';
  if (reason === 'missing_key') return 'The site operator needs to connect ElevenLabs before live captions are available.';
  if (reason === 'runtime_unavailable') return 'The live caption service is not ready. Please try again later.';
  if (['caption_runtime_missing', 'caption_worker_unavailable', 'caption_worker_ended'].includes(reason)) return 'The live caption service needs attention from the site operator.';
  if (reason === 'source_identity_unverified') return 'Live captions are unavailable because this source could not be verified.';
  if (['budget_exhausted', 'budget_deadline', 'audio_limit', 'session_limit', 'deadline', 'time_limit', 'budget_limit'].includes(reason)) return 'Live caption session allowance has been reached.';
  if (['rate_limited', 'rate_limit', 'quota_exceeded'].includes(reason)) return 'Live captions are temporarily limited by the provider. Please try again later.';
  if (['insufficient_credits', 'insufficient_credit', 'credits_exhausted', 'account_ineligible'].includes(reason)) return 'Live captions are unavailable for this provider account.';
  if (captionSessionEnded(value)) return 'This replay session has ended. Start a new replay session for live captions.';
  if (!connected) return 'The replay connection is unavailable. Reconnecting automatically; no live captions are being received.';
  if (value?.status === 'paused') return 'Live captions pause with the player.';
  if (['source_preparing', 'awaiting_playback', 'playback_inactive', 'seek'].includes(reason)
    || ['stalled', 'buffering', 'waiting_for_playback'].includes(value?.status)) return 'Live captions are waiting for the player to resume.';
  if (['unavailable', 'error', 'failed'].includes(value?.status) || value?.available === false) return 'Live commentary captions are unavailable right now.';
  if (!canControl && !value?.enabled) return 'Another viewer controls live caption streaming.';
  if (['connecting', 'starting', 'warming', 'waiting'].includes(value?.status)) return 'Connecting live commentary captions…';
  if (value?.status === 'stopped') return 'Live caption streaming has stopped. Check the replay and caption service status.';
  if (['listening', 'partial', 'committed', 'live', 'streaming', 'running'].includes(value?.status)) return 'Listening to the focused match…';
  return value?.enabled ? 'Waiting for live commentary captions…' : 'Live captions are off.';
}

// The backend session advertises each new generation before it publishes text.
// A partial has no invented word times. Its identity and PCM source anchor still
// have to agree with the actual selected media and authoritative generation.
export function readLiveCaption(message, session, feed, playback, minimumGeneration = 0) {
  const context = session?.captions;
  if (!context || message?.type !== 'caption' || message.session_id !== session.session_id
    || !Number.isSafeInteger(message.sequence) || message.sequence <= session.sequence
    || !Number.isSafeInteger(message.generation) || message.generation < minimumGeneration
    || message.generation !== context.generation || message.clock_epoch !== session.playback.clock_epoch
    || message.clock_epoch !== context.clock_epoch || message.match_id !== feed?.cameraId
    || message.match_id !== context.match_id || message.source_key !== feed.sourceKey
    || (feed.sourceIdentity && message.source_identity !== feed.sourceIdentity)
    || message.provider !== 'elevenlabs' || message.model !== LIVE_CAPTION_MODEL || !STATES.has(message.state)) return null;
  const hasText = message.state === 'partial' || message.state === 'committed';
  if (hasText && (!context.enabled || playback.stalled || playback.available === false || !playback.playing || playback.ended
    || !Number.isFinite(playback.sourceTime) || typeof message.text !== 'string' || message.text.length > 4000
    || !Number.isFinite(message.source_anchor_seconds) || message.source_anchor_seconds < 0
    || message.source_anchor_seconds > playback.sourceTime + .05)) return null;
  if (hasText && message.words !== undefined && (!Array.isArray(message.words) || message.words.length > 512
    || message.words.some((word) => !word || typeof word.text !== 'string' || !Number.isFinite(word.start_seconds)
      || !Number.isFinite(word.end_seconds) || word.start_seconds < 0 || word.end_seconds < word.start_seconds
      || word.end_seconds > playback.sourceTime + .25))) return null;
  return { ...message, status: message.state, text: hasText ? message.text : '', receivedLocal: Date.now() };
}
