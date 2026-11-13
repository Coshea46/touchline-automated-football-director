export default function startupFailure(replay) {
  if (replay.visitor.phase === 'ended') return replay.visitor.notice || 'This replay session ended. Start a new replay to reconnect.';
  if (replay.sessionState?.state === 'error') {
    const reason = replay.sessionState.error_code === 'inference_unavailable'
      ? 'The Director could not connect to its AI workers.'
      : 'The AI session stopped before setup finished.';
    return `${reason} Your matches have not started. You can try again or continue without AI.`;
  }
  const messages = {
    budget_exhausted: 'Today’s AI spending allowance has been reached. It resets at 00:00 UTC. You can continue watching without AI.',
    rate_limited: 'Too many AI starts from this connection. Please wait before trying again, or continue without AI.',
    ai_busy: 'Your previous AI session is still stopping. Please wait before trying again.',
    server_unavailable: 'The AI service is unavailable right now. You can continue watching without AI.',
    ai_finished: 'This AI run has ended. You can continue watching without AI.',
  };
  return messages[replay.sessionState?.ai_reason] || '';
}
