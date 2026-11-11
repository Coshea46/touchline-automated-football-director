import { useEffect, useRef, useState } from 'react';

export const SOURCE_READY_EVENT = 'matchpulse-source-ready';
export const SOURCE_LOAD_EVENT = 'matchpulse-source-load';

// Read the actual native media clock. This observer never seeks or drives playback.
export default function useSourcePlayback(mediaRef, source, enabled = true, identity = null) {
  const [frame, setFrame] = useState({ source: null, time: null });
  const lastSource = useRef(null);
  const descriptor = useRef(null);
  descriptor.current = { source, identity };
  const observation = identity || source;
  useEffect(() => {
    const video = mediaRef.current;
    const { source, identity } = descriptor.current;
    const expected = source.split('\n')[0];
    const cameraId = source.split('\n')[1];
    const changed = lastSource.current !== null && lastSource.current !== observation;
    const sameUrl = lastSource.current?.split('\n')[0] === expected;
    lastSource.current = observation;
    if (!video || !enabled) return undefined;
    let ready = false;
    const clear = () => { ready = false; setFrame({ source: observation, time: null }); };
    const sample = () => {
      if (!ready || video.seeking || video.readyState < 2 || !Number.isFinite(video.currentTime)) return;
      if (identity && 'matchpulseReadyIdentity' in video && video.matchpulseReadyIdentity !== identity) return;
      setFrame((previous) => previous.source === observation && previous.time === video.currentTime
        ? previous : { source: observation, time: video.currentTime });
    };
    const loaded = () => {
      const managedIdentity = identity && 'matchpulseReadyIdentity' in video;
      const managedReady = managedIdentity ? video.matchpulseReadyIdentity === identity
        : !('matchpulseReadySource' in video) || video.matchpulseReadySource === `${expected}\n${cameraId}`;
      ready = managedReady && (managedIdentity || video.getAttribute('src') === expected) && video.readyState >= 2 && !video.seeking;
      sample();
    };
    clear();
    const listeners = { loadeddata: loaded, loadedmetadata: loaded, canplay: loaded, seeked: loaded,
      timeupdate: sample, playing: loaded, pause: sample, seeking: clear, emptied: clear, error: clear, waiting: clear,
      loadstart: clear, [SOURCE_LOAD_EVENT]: clear, [SOURCE_READY_EVENT]: loaded };
    Object.entries(listeners).forEach(([event, handler]) => video.addEventListener(event, handler));
    // Enabling captions on an already playing source should show its current cue.
    // A different source is identified by its URL, never by the old readyState.
    if (!changed || sameUrl || video.matchpulseReadyIdentity === identity || video.matchpulseReadySource === `${expected}\n${cameraId}`) loaded();
    const timer = setInterval(sample, 100);
    return () => { clearInterval(timer); Object.entries(listeners).forEach(([event, handler]) => video.removeEventListener(event, handler)); };
  }, [mediaRef, observation, enabled]);
  return enabled && frame.source === observation ? frame.time : null;
}
