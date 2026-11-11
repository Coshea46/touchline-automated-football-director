import { useCallback, useLayoutEffect, useRef, useState } from 'react';
import { createAudioOwnership } from './audioOwnership';

// The clock keeps the viewer's desired mute preference. Forced ownership mute
// changes only the media output, never playback position, rate, source or intent.
export default function useAudioOwnership({ media, clock, sourceId, enabled = true }) {
  const coordinator = useRef(null);
  const activeMedia = useRef(null);
  const allowed = useRef(enabled);
  allowed.current = enabled;
  const [state, setState] = useState({ owned: false, pending: false, status: enabled ? 'requesting' : 'other-tab' });

  useLayoutEffect(() => {
    let active = true;
    const broker = createAudioOwnership({ onChange: (next) => {
      // A lease belongs to this tab, not to whichever pool node was focused
      // when it was acquired. Resolve the current node for every ownership change.
      const video = activeMedia.current;
      if (next.owned && !allowed.current) {
        if (video) { video.matchpulseAudioOwned = false; video.muted = true; }
        coordinator.current?.release();
        return;
      }
      if (next.owned && next.explicit) clock.current.muted = false;
      if (video) {
        video.matchpulseAudioOwned = next.owned;
        video.muted = !next.owned || Boolean(clock.current.muted);
      }
      if (active) setState({ owned: next.owned, pending: next.pending, status: next.status });
    } });
    coordinator.current = broker;
    if (allowed.current) broker.request({ explicit: false });
    return () => {
      active = false;
      const video = activeMedia.current;
      if (video) { video.matchpulseAudioOwned = false; video.muted = true; }
      broker.dispose(); // Mute first, then relinquish the held lock.
      if (coordinator.current === broker) coordinator.current = null;
    };
  }, [media, clock]);

  useLayoutEffect(() => {
    const video = media.current;
    const previous = activeMedia.current;
    const forceMute = (target = video) => {
      if (!target) return;
      target.matchpulseAudioOwned = false;
      if (!target.muted) target.muted = true;
    };
    if (previous && previous !== video) forceMute(previous);
    activeMedia.current = video;
    if (!video) return undefined;
    forceMute();
    let gestureUntil = 0;
    const gesture = (event) => {
      if (event.type === 'pointerdown' || ['m', 'M', 'ArrowUp', 'ArrowDown', 'Enter', ' '].includes(event.key)) {
        gestureUntil = Date.now() + 1000;
      }
    };
    const guard = (event) => {
      const broker = coordinator.current;
      if (!allowed.current) { forceMute(); return; }
      if (activeMedia.current !== video) { forceMute(); return; }
      if (broker?.owned || video.muted) return;
      const requested = event.type === 'volumechange' && video.volume > 0 && Date.now() <= gestureUntil;
      forceMute();
      if (requested) {
        gestureUntil = 0;
        clock.current.muted = false;
        broker?.request({ explicit: true });
      }
    };
    video.addEventListener('pointerdown', gesture, true);
    video.addEventListener('keydown', gesture, true);
    ['volumechange', 'play', 'playing', 'loadedmetadata'].forEach((type) => video.addEventListener(type, guard, true));
    if (coordinator.current?.owned && allowed.current) {
      video.matchpulseAudioOwned = true;
      video.muted = Boolean(clock.current.muted);
    }
    return () => {
      forceMute();
      if (activeMedia.current === video) activeMedia.current = null;
      video.removeEventListener('pointerdown', gesture, true);
      video.removeEventListener('keydown', gesture, true);
      ['volumechange', 'play', 'playing', 'loadedmetadata'].forEach((type) => video.removeEventListener(type, guard, true));
    };
  }, [media, clock, sourceId]);

  useLayoutEffect(() => {
    const broker = coordinator.current;
    if (!enabled) {
      const video = activeMedia.current;
      if (video) { video.matchpulseAudioOwned = false; video.muted = true; }
      broker?.release();
    } else if (!broker?.owned) broker?.request({ explicit: false });
  }, [enabled]);

  const requestAudio = useCallback(() => allowed.current
    ? coordinator.current?.request({ explicit: true }) || Promise.resolve(false) : Promise.resolve(false), []);
  const releaseAudio = useCallback(() => coordinator.current?.release(), []);
  return { ...state, requestAudio, releaseAudio };
}
