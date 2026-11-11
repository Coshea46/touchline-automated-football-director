export default function directorViewState({ connection, sessionState, backendError, autoSwitch,
  holdUntil, director, uiNow, serverSkew, feeds }) {
  const autoAvailable = connection === 'connected' && Boolean(sessionState)
    && ['real', 'features_mock'].includes(sessionState.mode)
    && (sessionState.auto_available ?? ['running', 'degraded', 'paused'].includes(sessionState.state));
  const autoCanStart = connection === 'connected' && sessionState?.auto_can_start === true;
  const autoStarting = connection === 'connected' && sessionState?.auto_starting === true;
  const autoToggleable = connection === 'connected' && sessionState?.controls_enabled !== false
    && (autoAvailable || autoCanStart || autoStarting);
  const held = autoAvailable && autoSwitch && holdUntil > 0;
  const autoLabel = !(autoAvailable || autoCanStart || autoStarting) ? 'Auto unavailable' : autoStarting && autoSwitch ? 'Auto starting'
    : !autoAvailable || !autoSwitch ? 'Auto off' : held ? 'Auto paused' : 'Auto';
  const startReasons = {
    credentials_unavailable: 'Auto Director needs inference credentials on the backend.',
    model_unavailable: 'Auto Director could not load the configured model. Ask the site operator to check the model setup.',
    budget_exhausted: 'The monitoring allowance has been used.',
    session_expired: 'The monitoring session allowance has ended.',
    source_ended: 'The replay has ended.',
    not_prepared: 'Monitoring is not enabled on this backend.',
    closing: 'The backend is shutting down.',
  };
  const autoTitle = connection === 'connected' && sessionState?.controls_enabled === false
    ? sessionState.can_claim_control === true ? 'Another tab controls this replay. Use this tab to take control.'
      : 'Another tab controls this replay. Restart the backend to enable control handoff.'
    : !autoToggleable ? startReasons[sessionState?.auto_start_reason] || 'Auto Director needs a ready monitoring session'
    : autoCanStart ? 'Start a bounded real-model monitoring session'
    : autoStarting && autoSwitch ? 'Monitoring is starting. Click to turn automatic switching off.'
    : !autoSwitch ? 'Turn on automatic switching'
    : held ? 'Paused after your pick; resumes shortly. Click to turn off'
      : 'Automatically switching to the most interesting match. Click to turn off';
  const waitingForStart = sessionState?.auto_can_start === true && sessionState?.auto_starting !== true;
  const inferenceBuffering = connection === 'connected' && !waitingForStart
    && (sessionState?.auto_starting === true || ['waiting', 'starting'].includes(sessionState?.state));
  const inferenceLabel = backendError || (sessionState?.controls_enabled === false ? 'Viewing only'
    : inferenceBuffering ? 'Starting inference'
    : sessionState && connection !== 'connected' ? 'Inference offline'
    : autoCanStart ? 'Ready to start monitoring'
    : startReasons[sessionState?.auto_start_reason] && ['disabled', 'stopped'].includes(sessionState?.state)
      ? startReasons[sessionState.auto_start_reason]
    : sessionState?.state === 'disabled' ? 'Monitoring off'
    : sessionState?.state === 'stopped' ? 'Monitoring session ended'
    : sessionState?.state === 'ended' ? 'Replay ended'
      : ['failed', 'error', 'unavailable'].includes(sessionState?.state) ? 'Inference unavailable'
      : sessionState?.mode === 'features_mock' ? 'Mock scores'
        : sessionState?.mode === 'real' ? 'Trained model' : 'No inference session');
  const now = uiNow / 1000 + serverSkew;
  const monitoring = connection === 'connected' && ['running', 'degraded'].includes(sessionState?.state);
  const mode = director?.mode || 'balanced';
  const scoreFresh = Boolean(monitoring && director && director.scores_fresh !== false
    && Number.isFinite(director.scores_produced_at) && now - director.scores_produced_at <= 6
    && (!Number.isFinite(director.scores_valid_until) || director.scores_valid_until > now));
  const scores = scoreFresh ? Object.fromEntries(Object.entries(director.latest_scores)
    .filter(([id]) => feeds.some((feed) => feed.cameraId === id && feed.healthy !== false))) : {};
  return { autoAvailable, autoCanStart, autoStarting, autoToggleable, held, autoLabel, autoTitle,
    inferenceBuffering, inferenceLabel, now, monitoring, mode, scoreFresh, scores };
}
