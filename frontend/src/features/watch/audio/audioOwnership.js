// Web Locks provide exclusive ownership. Messages only request cooperative yield;
// no lease expiry or lock stealing can make a still-audible old owner overlap.
// https://w3c.github.io/web-locks/#api-lock-manager-request
export const AUDIO_LOCK = 'matchpulse-audible-player-v1';
export const AUDIO_CHANNEL = 'matchpulse-audio-v1';
export const AUDIO_REQUEST_KEY = 'matchpulse-audio-request-v1';
export const AUDIO_REQUEST_TIMEOUT_MS = 3000;
const RELEASE_BARRIER = Symbol.for('matchpulse.audio-release-barrier.v1');

function newId() {
  return window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function createAudioOwnership({ browser = window, onChange = () => {}, id = newId(),
  timeoutMs = AUDIO_REQUEST_TIMEOUT_MS } = {}) {
  let locks;
  let storage;
  let channel;
  try { locks = browser.navigator?.locks; } catch {}
  try { storage = browser.localStorage; } catch {}
  try { if (browser.BroadcastChannel) channel = new browser.BroadcastChannel(AUDIO_CHANNEL); } catch {}
  let closed = false;
  let owned = false;
  let pending = null;
  let releaseHeld = null;
  let ownerClaim = null;
  let counter = 0;
  let lockLifetime = Promise.resolve();

  const notify = (status, explicit = false) => onChange({ owned, pending: Boolean(pending), status, explicit });
  const newer = (left, right) => !right || left.at > right.at || (left.at === right.at && left.token > right.token);
  const release = (status = 'other-tab') => {
    if (pending) {
      const request = pending;
      pending = null;
      request.cancelled = true;
      clearTimeout(request.timer);
      request.controller?.abort();
      request.finish(false);
    }
    owned = false;
    // The owner callback MUST synchronously mute the media before releasing.
    notify(status);
    releaseHeld?.();
    releaseHeld = null;
    ownerClaim = null;
  };
  const receive = (value) => {
    if (closed || !value || value.version !== 1 || value.type !== 'takeover' || value.id === id
      || typeof value.id !== 'string' || value.id.length > 100
      || typeof value.token !== 'string' || value.token.length > 150
      || !Number.isFinite(value.at) || Math.abs(Date.now() - value.at) > 10000) return;
    // A later user gesture can share a coarse clock tick with the current
    // owner's acquisition. It still requests a cooperative yield, irrespective
    // of tab-ID ordering. Pending simultaneous requests keep their tie-break.
    if ((owned && (!ownerClaim || value.at >= ownerClaim.at)) || (pending && newer(value, pending.claim))) release();
  };
  const message = (event) => receive(event.data);
  const stored = (event) => {
    if (event.key !== AUDIO_REQUEST_KEY || typeof event.newValue !== 'string' || event.newValue.length > 1024) return;
    try { receive(JSON.parse(event.newValue)); } catch {}
  };
  const pagehide = () => release();
  channel?.addEventListener('message', message);
  browser.addEventListener('storage', stored);
  browser.addEventListener('pagehide', pagehide);

  const request = ({ explicit = true } = {}) => {
    if (closed) return Promise.resolve(false);
    if (owned) {
      if (explicit) notify('owned', true);
      return Promise.resolve(true);
    }
    if (pending) {
      if (!explicit || pending.explicit) return pending.promise;
      release(); // A native audio gesture can replace the initial nonblocking attempt.
    }
    if (!locks || typeof locks.request !== 'function') {
      notify('unavailable');
      return Promise.resolve(false);
    }
    const claim = { version: 1, type: 'takeover', id, at: Date.now(), token: `${id}:${++counter}` };
    let finish;
    const promise = new Promise((resolve) => { finish = resolve; });
    const item = { promise, finish, claim, explicit, cancelled: false, controller: null, timer: null };
    pending = item;
    notify('requesting', explicit);
    let options;
    if (explicit) {
      item.controller = new AbortController();
      options = { mode: 'exclusive', signal: item.controller.signal };
      item.timer = setTimeout(() => {
        if (pending === item) release('other-tab');
      }, timeoutMs);
      try { channel?.postMessage(claim); } catch {}
      try { storage?.setItem(AUDIO_REQUEST_KEY, JSON.stringify(claim)); } catch {}
    } else {
      // The spec forbids combining ifAvailable with an AbortSignal. An abandoned
      // nonblocking callback is instead guarded by closed/item identity below.
      options = { mode: 'exclusive', ifAvailable: true };
    }
    try {
      // StrictMode/HMR can remount before this page's prior callback has fully
      // released its lock. Wait for that lifecycle only, never another tab.
      lockLifetime = Promise.resolve(browser[RELEASE_BARRIER]).then(() => {
        if (closed || pending !== item || item.cancelled) return undefined;
        return locks.request(AUDIO_LOCK, options, async (lock) => {
          if (closed || pending !== item || item.cancelled) return;
          clearTimeout(item.timer);
          pending = null;
          if (!lock) {
            notify('other-tab');
            finish(false);
            return;
          }
          owned = true;
          ownerClaim = claim;
          const lifetime = new Promise((resolve) => { releaseHeld = resolve; });
          notify('owned', explicit);
          finish(true);
          await lifetime;
        });
      }).catch(() => {
        if (pending === item) {
          pending = null;
          clearTimeout(item.timer);
          notify('unavailable');
          finish(false);
        } else if (owned && ownerClaim === claim) release('unavailable');
      });
    } catch {
      if (pending === item) {
        pending = null;
        clearTimeout(item.timer);
        notify('unavailable');
        finish(false);
      }
    }
    return promise;
  };

  return {
    request,
    release: () => release(),
    get owned() { return owned; },
    dispose() {
      if (closed) return;
      closed = true;
      release('unavailable');
      browser[RELEASE_BARRIER] = Promise.allSettled([browser[RELEASE_BARRIER], lockLifetime]);
      channel?.removeEventListener('message', message);
      channel?.close();
      browser.removeEventListener('storage', stored);
      browser.removeEventListener('pagehide', pagehide);
    },
  };
}
