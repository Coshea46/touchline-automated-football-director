import { useCallback, useRef, useState } from 'react';

export const INDEPENDENT_SESSIONS = process.env.REACT_APP_INDEPENDENT_SESSIONS === 'true';
export const VISITOR_STORAGE_KEY = 'touchline-visitor-session-v1';

const safeCredential = (value) => typeof value === 'string' && /^[A-Za-z0-9_-]{16,128}$/.test(value);
const validCapability = (value) => value && safeCredential(value.session_id) && safeCredential(value.resume_token)
  && Number.isFinite(value.expires_at) && value.expires_at > 0;
const terminal = {
  session_expired: 'This replay session has ended. Start a new replay from the beginning.',
  resume_rejected: 'This saved replay cannot be resumed. Start a new replay from the beginning.',
  session_replaced: 'This replay is open in another tab. Start a new replay here.',
  invalid_hello: 'The replay connection could not be authorized. Start a new replay.',
  hello_timeout: 'The replay connection could not be authorized. Start a new replay.',
};
const closeCodes = { 4400: 'invalid_hello', 4408: 'hello_timeout', 4403: 'resume_rejected',
  4404: 'session_expired', 4409: 'session_replaced', 4413: 'capacity_reached', 4429: 'rate_limited' };

function storedCapability(enabled) {
  if (!enabled) return { capability: null, invalid: false };
  let raw;
  try {
    raw = window.sessionStorage.getItem(VISITOR_STORAGE_KEY);
  } catch {
    return { capability: null, invalid: false };
  }
  if (!raw) return { capability: null, invalid: false };
  try {
    const parsed = raw.length <= 2048 ? JSON.parse(raw) : null;
    return validCapability(parsed) ? { capability: parsed, invalid: false } : { capability: null, invalid: true };
  } catch {
    return { capability: null, invalid: true };
  }
}

// Capabilities stay in this tab's sessionStorage and private closures. They are
// never included in a URL, rendered props, permanent storage or console output.
export default function useVisitorSession(enabled = INDEPENDENT_SESSIONS) {
  const initial = useRef(null);
  if (!initial.current) initial.current = storedCapability(enabled);
  const capability = useRef(initial.current.capability);
  const [generation, setGeneration] = useState(0);
  const [info, setInfo] = useState(() => ({ phase: initial.current.invalid ? 'ended' : 'connecting',
    notice: initial.current.invalid ? terminal.resume_rejected : '', retryAt: null,
    sessionId: initial.current.capability?.session_id, expiresAt: initial.current.capability?.expires_at }));
  const infoRef = useRef(info);
  infoRef.current = info;

  const onOpen = useCallback(() => {
    setInfo((previous) => ({ ...previous, phase: 'connecting', notice: 'Connecting your replay…', retryAt: null }));
  }, []);
  const createHello = useCallback(() => capability.current
    ? { type: 'hello', session_id: capability.current.session_id, resume_token: capability.current.resume_token }
    : { type: 'hello' }, []);
  const onError = useCallback((message, { authenticated = false } = {}) => {
    const code = message?.code;
    if (terminal[code]) {
      setInfo((previous) => ({ ...previous, phase: 'ended', notice: terminal[code], retryAt: null }));
      return { halt: true };
    }
    if (!authenticated && ['rate_limited', 'capacity_reached'].includes(code)) {
      const seconds = Number.isFinite(message.retry_after) ? Math.max(1, Math.min(300, message.retry_after)) : 10;
      setInfo((previous) => ({ ...previous, phase: 'waiting', retryAt: Math.max(previous.retryAt || 0, Date.now() + seconds * 1000),
        notice: code === 'rate_limited' ? 'Too many replay requests. Please wait before trying again.'
          : 'All replay places are in use. Retrying shortly.' }));
      return { retryAfterMs: seconds * 1000 };
    }
    return null;
  }, []);
  const onAuth = useCallback((message) => {
    const previous = capability.current;
    if (!validCapability(message) || typeof message.resumed !== 'boolean'
      || (previous && (message.session_id !== previous.session_id || !message.resumed))
      || (!previous && message.resumed)) {
      onError({ code: 'resume_rejected' });
      return false;
    }
    const next = { session_id: message.session_id, resume_token: message.resume_token, expires_at: message.expires_at };
    capability.current = next;
    try { window.sessionStorage.setItem(VISITOR_STORAGE_KEY, JSON.stringify(next)); } catch { /* Memory fallback. */ }
    setInfo({ phase: 'ready', notice: '', retryAt: null, sessionId: next.session_id, expiresAt: next.expires_at });
    return true;
  }, [onError]);
  const onClose = useCallback((event) => {
    const action = onError({ code: closeCodes[event?.code] });
    if (!action && infoRef.current.phase !== 'waiting' && infoRef.current.phase !== 'ended') {
      setInfo((previous) => ({ ...previous, phase: 'offline', notice: 'Connection lost. Resuming your replay…' }));
    }
    return action;
  }, [onError]);
  const restart = useCallback(() => {
    capability.current = null;
    try { window.sessionStorage.removeItem(VISITOR_STORAGE_KEY); } catch { /* Memory fallback. */ }
    setInfo({ phase: 'connecting', notice: 'Starting a new replay…', retryAt: null });
    setGeneration((value) => value + 1);
  }, []);

  return { enabled, ...info, restart,
    socketOptions: enabled ? { createHello, onOpen, onAuth, onError, onClose, generation, blocked: info.phase === 'ended' } : undefined };
}
