import { useEffect, useRef, useState } from 'react';

export const SOURCE_READY_EVENT = 'matchpulse-source-ready';
export const SOURCE_LOAD_EVENT = 'matchpulse-source-load';

// Read the actual native media clock. This observer never seeks or drives playback.
export default function useSourcePlayback(mediaRef, source, enabled = true) {
  const [frame, setFrame] = useState({ source: null, time: null });
  const lastSource = useRef(null);
  useEffect(() => {
    const video = mediaRef.current;
    const expected = source.split('\n')[0];
    const cameraId = source.split('\n')[1];
    const changed = lastSource.current !== null && lastSource.current !== source;
    const sameUrl = lastSource.current?.split('\n')[0] === expected;
    lastSource.current = source;
    if (!video || !enabled) return undefined;
    let ready = false;
    const clear = () => { ready = false; setFrame({ source, time: null }); };
    const sample = () => {
      if (!ready || video.seeking || video.readyState < 2 || !Number.isFinite(video.currentTime)) return;
      setFrame((previous) => previous.source === source && previous.time === video.currentTime
        ? previous : { source, time: video.currentTime });
    };
    const loaded = () => {
      const managedReady = !('matchpulseReadySource' in video) || video.matchpulseReadySource === `${expected}\n${cameraId}`;
      ready = managedReady && video.getAttribute('src') === expected && video.readyState >= 2 && !video.seeking;
      sample();
    };
    clear();
    const listeners = { loadeddata: loaded, loadedmetadata: loaded, canplay: loaded, seeked: loaded,
      timeupdate: sample, playing: loaded, pause: sample, seeking: clear, emptied: clear, error: clear, waiting: clear,
      loadstart: clear, [SOURCE_LOAD_EVENT]: clear, [SOURCE_READY_EVENT]: loaded };
    Object.entries(listeners).forEach(([event, handler]) => video.addEventListener(event, handler));
    // Enabling captions on an already playing source should show its current cue.
    // A different source is identified by its URL, never by the old readyState.
    if (!changed || sameUrl || video.matchpulseReadySource === `${expected}\n${cameraId}`) loaded();
    const timer = setInterval(sample, 100);
    return () => { clearInterval(timer); Object.entries(listeners).forEach(([event, handler]) => video.removeEventListener(event, handler)); };
  }, [mediaRef, source, enabled]);
  return enabled && frame.source === source ? frame.time : null;
}
