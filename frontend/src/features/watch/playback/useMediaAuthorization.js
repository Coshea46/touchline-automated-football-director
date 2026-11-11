import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { getVideoUrl, playbackIdentity } from '../sources/feeds';

export const URL_RENEWAL_LEAD_MS = 30000;

const descriptor = (feed) => ({ identity: playbackIdentity(feed), url: getVideoUrl(feed),
  expiresAt: feed.urlExpiresAtLocal ?? feed.urlExpiresAt });
const fresh = (source) => source.url && (!Number.isFinite(source.expiresAt) || source.expiresAt * 1000 > Date.now());

// New credentials do not replace a healthy decoder. Keep the newest authorization
// for recovery, then adopt it on the same element shortly before the active URL expires.
export default function useMediaAuthorization(feed, clock) {
  const candidate = useMemo(() => descriptor(feed), [feed]);
  const latest = useRef(candidate);
  latest.current = candidate;
  const [selected, setSelected] = useState(() => fresh(candidate) ? candidate : { ...candidate, url: '' });
  const active = selected.identity === candidate.identity ? selected : fresh(candidate) ? candidate : { ...candidate, url: '' };
  const current = useRef(active);
  current.current = active;
  const needsRecovery = useRef(false);
  const adopt = useCallback((next, sample) => {
    if (sample) clock.current.sample?.();
    needsRecovery.current = false;
    current.current = next;
    setSelected(next);
  }, [clock]);
  const recover = useCallback(() => {
    needsRecovery.current = true;
    const next = latest.current;
    if (next.identity !== current.current.identity || next.url === current.current.url || !fresh(next)) return false;
    adopt(next, false);
    return true;
  }, [adopt]);

  useLayoutEffect(() => {
    if (selected.identity !== candidate.identity || (!selected.url && fresh(candidate))) {
      adopt(fresh(candidate) ? candidate : { ...candidate, url: '' }, false);
    } else if (needsRecovery.current && candidate.url !== selected.url && fresh(candidate)) {
      adopt(candidate, false);
    } else if (candidate.url === selected.url && candidate.expiresAt !== selected.expiresAt) {
      adopt(candidate, false);
    }
  }, [candidate, selected, adopt]);

  useLayoutEffect(() => {
    if (!active.url || !Number.isFinite(active.expiresAt)) return undefined;
    let timer;
    const renew = () => {
      const next = latest.current;
      if (next.identity === current.current.identity && next.url !== current.current.url && fresh(next)) {
        adopt(next, true);
      } else {
        // Registration updates arrive over the existing backend socket; no browser signing request.
        timer = setTimeout(renew, 1000);
      }
    };
    timer = setTimeout(renew, Math.max(0, active.expiresAt * 1000 - Date.now() - URL_RENEWAL_LEAD_MS));
    return () => clearTimeout(timer);
  }, [active.url, active.identity, active.expiresAt, adopt]);

  return { url: active.url, identity: active.identity, recover };
}
