import { useEffect, useRef, useState } from 'react';
import { BACKEND_CONNECTED_EVENT } from './connectionEvents';

export const API_URL = (process.env.REACT_APP_API_URL || 'http://127.0.0.1:8031').replace(/\/$/, '');
export const API_REQUEST_TIMEOUT_MS = 8000;
export const API_RECOVERY_EVENT = BACKEND_CONNECTED_EVENT;
const cache = new Map();

export class PlatformConnectionError extends Error {
  constructor() {
    super('The Touchline data service could not be reached. Retrying automatically. Check that the backend is running.');
    this.name = 'PlatformConnectionError';
    this.code = 'backend_unavailable';
  }
}

export const isPlatformConnectionError = (error) => error?.code === 'backend_unavailable';

// Retry only a failed connection, never a provider missing-key/quota response.
// Visible pages own requests; this hook cancels its timers when they are hidden.
export function useApiRecovery(active, failed, busy = false) {
  const [revision, setRevision] = useState(0);
  const attempts = useRef(0);
  const lastRetry = useRef(-Infinity);
  useEffect(() => {
    if (!failed) attempts.current = 0;
    if (!active || !failed || busy) return undefined;
    const retry = () => {
      if (Date.now() - lastRetry.current < 1000) return;
      lastRetry.current = Date.now();
      attempts.current += 1;
      setRevision((value) => value + 1);
    };
    const timer = setTimeout(retry, Math.min(30000, 5000 * 2 ** Math.min(attempts.current, 3)));
    const events = ['online', 'focus', API_RECOVERY_EVENT];
    events.forEach((event) => window.addEventListener(event, retry));
    return () => { clearTimeout(timer); events.forEach((event) => window.removeEventListener(event, retry)); };
  }, [active, failed, busy, revision]);
  return revision;
}

export function clearApiCache() { cache.forEach((entry) => clearTimeout(entry.timer)); cache.clear(); }

export async function apiGet(path, parameters = {}, refresh = false) {
  const query = new URLSearchParams(Object.entries(parameters).filter(([, value]) => value !== '' && value != null));
  const encoded = query.toString();
  const url = `${API_URL}${path}${encoded ? `?${encoded}` : ''}`;
  const previous = cache.get(url);
  if (!refresh && previous && previous.expires > Date.now()) return previous.promise;
  if (previous) clearTimeout(previous.timer);
  const entry = { expires: Date.now() + 60000 };
  const promise = Promise.resolve().then(async () => {
    if (typeof fetch !== 'function') throw new PlatformConnectionError();
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timeout = controller ? setTimeout(() => controller.abort(), API_REQUEST_TIMEOUT_MS) : null;
    let response;
    let body;
    try {
      try {
        response = await fetch(url, { headers: { Accept: 'application/json' }, ...(controller ? { signal: controller.signal } : {}) });
      } catch {
        // Browser network errors can contain internal addresses. Do not expose them.
        throw new PlatformConnectionError();
      }
      try { body = await response.json(); }
      catch (reason) {
        if (controller?.signal.aborted || reason?.name === 'AbortError' || reason instanceof TypeError) throw new PlatformConnectionError();
        throw new Error('The Touchline data service returned an invalid response. Check the backend connection and try again.');
      }
    } finally { clearTimeout(timeout); }
    if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('The football service returned an invalid response.');
    if (!response.ok && !body.status) throw new Error('Football data could not be loaded. Please try again.');
    if (path === '/api/news') {
      const hardExpiry = Number.isFinite(body.content_expires_at) ? body.content_expires_at * 1000
        : Number.isFinite(body.fetched_at) ? (body.fetched_at + 24 * 60 * 60) * 1000 : entry.expires;
      entry.expires = Math.min(entry.expires, hardExpiry);
    }
    if (path === '/api/captions' && Number.isFinite(body.source_identity_valid_until)) {
      entry.expires = Math.min(entry.expires, body.source_identity_valid_until * 1000);
    }
    // Purge cached metadata even if this topic is no longer the visible page.
    entry.timer = setTimeout(() => { if (cache.get(url) === entry) cache.delete(url); }, Math.max(0, entry.expires - Date.now()));
    return body;
  });
  entry.promise = promise;
  cache.set(url, entry);
  if (cache.size > 64) { const oldest = cache.keys().next().value; clearTimeout(cache.get(oldest).timer); cache.delete(oldest); }
  try { return await promise; } catch (error) { if (cache.get(url) === entry) cache.delete(url); throw error; }
}

export function safeWebUrl(value) {
  try { const url = new URL(value); return url.protocol === 'https:' ? url.href : null; } catch { return null; }
}

export function updatedLabel(timestamp) {
  if (!Number.isFinite(timestamp)) return 'Update time unavailable';
  return `Updated ${new Date(timestamp * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}`;
}
