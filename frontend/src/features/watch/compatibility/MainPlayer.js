import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { getVideoUrl, sourceOffset } from '../sources/feeds';
import { SOURCE_LOAD_EVENT, SOURCE_READY_EVENT } from '../playback/useSourcePlayback';

export function boundedTime(time, duration) {
  const safe = Number.isFinite(time) ? Math.max(0, time) : 0;
  return Number.isFinite(duration) && safe >= duration ? Math.max(0, duration - 0.1) : safe;
}

// The DOM element survives every selection. Only a genuinely different URL loads.
export default function MainPlayer({ feed, clock, seekTo, onPlayback, onAvailability, mediaRef, inferenceBuffering = false, audioAllowed = true }) {
  const ref = useRef(null);
  const appliedSeek = useRef(null);
  const appliedRetry = useRef(0);
  const source = getVideoUrl(feed);
  const offset = sourceOffset(feed);
  const callbacks = useRef({ onPlayback, onAvailability });
  const buffering = useRef(inferenceBuffering);
  const audioPermission = useRef(audioAllowed);
  const resumeAfterBuffer = useRef(false);
  buffering.current = inferenceBuffering;
  audioPermission.current = audioAllowed;
  callbacks.current = { onPlayback, onAvailability };
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);

  useLayoutEffect(() => {
    if (!mediaRef) return undefined;
    const video = ref.current;
    if (!('matchpulseReadySource' in video)) video.matchpulseReadySource = null;
    mediaRef.current = video;
    return () => { if (mediaRef.current === video) mediaRef.current = null; };
  }, [mediaRef]);

  useLayoutEffect(() => {
    ref.current.muted = !audioAllowed || ref.current.matchpulseAudioOwned === false || clock.current.muted;
  }, [audioAllowed, clock]);

  useEffect(() => {
    const video = ref.current;
    const replayClock = clock.current;
    let cancelled = false;
    let preparing = true;
    let failed = false;
    const retryRequested = retry !== appliedRetry.current;
    appliedRetry.current = retry;
    const resourceChanged = video.getAttribute('src') !== source;
    video.matchpulseReadySource = null;
    if (resourceChanged || retryRequested) video.dispatchEvent(new Event(SOURCE_LOAD_EVENT));
    const target = { ...clock.current };
    clock.current.stalled = true;
    clock.current.ended = false;
    callbacks.current.onPlayback?.();
    setLoading(true);
    setError(false);

    const remember = () => {
      if (preparing || failed) return;
      clock.current.time = Math.max(0, video.currentTime - offset);
      clock.current.rate = video.playbackRate;
      if (audioPermission.current && video.matchpulseAudioOwned !== false) clock.current.muted = video.muted;
      clock.current.volume = video.volume;
    };
    clock.current.sample = remember;
    const report = () => { remember(); callbacks.current.onPlayback?.(); };
    const fail = () => {
      if (cancelled) return;
      failed = true;
      video.matchpulseReadySource = null;
      clock.current.stalled = true;
      clock.current.available = false;
      setError(true);
      setLoading(false);
      callbacks.current.onAvailability?.(feed.cameraId, false);
      callbacks.current.onPlayback?.();
    };
    const ready = () => {
      if (cancelled || failed || !preparing || video.seeking || video.readyState < 2) return;
      preparing = false;
      clock.current.stalled = false;
      clock.current.available = true;
      setLoading(false);
      remember();
      video.matchpulseReadySource = `${source}\n${feed.cameraId}`;
      video.dispatchEvent(new Event(SOURCE_READY_EVENT));
      callbacks.current.onAvailability?.(feed.cameraId, true);
      callbacks.current.onPlayback?.();
      if ((target.playing || resumeAfterBuffer.current) && !buffering.current) {
        resumeAfterBuffer.current = false;
        const attempt = video.play();
        if (attempt?.catch) attempt.catch((reason) => {
          if (cancelled || reason?.name === 'AbortError') return;
          if (reason?.name === 'NotAllowedError') {
            video.muted = true;
            clock.current.muted = true;
            video.play()?.catch(() => { if (!cancelled) { clock.current.playing = false; report(); } });
          } else fail();
        });
      }
    };
    const metadata = () => {
      if (cancelled || !preparing) return;
      // An exhausted destination cannot move the shared replay clock backward.
      // Keep its previous position and expose the normal unavailable/retry UI.
      if (Number.isFinite(video.duration) && target.time + offset >= video.duration) {
        fail();
        return;
      }
      video.playbackRate = target.rate;
      video.volume = target.volume;
      video.muted = target.muted || !audioPermission.current || video.matchpulseAudioOwned === false;
      const time = boundedTime(target.time + offset, video.duration);
      clock.current.time = Math.max(0, time - offset);
      if (Math.abs(video.currentTime - time) > 0.05) video.currentTime = time;
      ready();
    };
    const playing = () => {
      if (buffering.current) {
        resumeAfterBuffer.current = true;
        clock.current.playing = false;
        video.pause();
        report();
        return;
      }
      if (!preparing) { clock.current.playing = true; clock.current.stalled = false; report(); }
    };
    const paused = () => { if (!preparing) { clock.current.playing = false; report(); } };
    const stalled = () => { clock.current.stalled = true; report(); };
    const seeking = () => { if (!preparing) { clock.current.stalled = true; report(); } };
    const resumed = () => {
      if (preparing) ready();
      else { clock.current.stalled = false; report(); }
    };
    const ended = () => { clock.current.playing = false; clock.current.ended = true; report(); };
    const listeners = {
      loadedmetadata: metadata, loadeddata: ready, canplay: resumed, seeked: resumed, error: fail,
      timeupdate: remember, seeking, ratechange: report,
      volumechange: remember, play: playing, playing, pause: paused,
      waiting: stalled, stalled, ended,
    };
    Object.entries(listeners).forEach(([event, handler]) => video.addEventListener(event, handler));
    // Set src imperatively after saving the old timeline, rather than letting a
    // React src mutation reset currentTime before the handoff can capture it.
    if (resourceChanged) video.src = source;
    else if (retryRequested) video.load();
    else if (video.readyState >= 1) metadata();

    return () => {
      remember();
      cancelled = true;
      if (replayClock.sample === remember) delete replayClock.sample;
      Object.entries(listeners).forEach(([event, handler]) => video.removeEventListener(event, handler));
    };
  }, [source, offset, feed.cameraId, clock, retry]);

  // Cold startup is an explicit replay buffer. Keep the exact media position
  // while the persistent worker opens its sources, then restore playback intent.
  useEffect(() => {
    const video = ref.current;
    let cancelled = false;
    if (inferenceBuffering) {
      resumeAfterBuffer.current = clock.current.playing;
      clock.current.playing = false;
      video.pause();
    } else if (resumeAfterBuffer.current) {
      clock.current.playing = true;
      if (video.readyState >= 2) {
        resumeAfterBuffer.current = false;
        video.play()?.catch((reason) => {
          if (cancelled || reason?.name === 'AbortError') return;
          if (reason?.name === 'NotAllowedError') {
            video.muted = true;
            clock.current.muted = true;
            video.play()?.catch(() => {
              if (!cancelled) { clock.current.playing = false; callbacks.current.onPlayback?.(); }
            });
          } else {
            clock.current.playing = false;
            callbacks.current.onPlayback?.();
          }
        });
      }
    }
    callbacks.current.onPlayback?.();
    return () => { cancelled = true; };
  }, [inferenceBuffering, clock]);

  // External seek (the backend's live timestamp). Declared after the effect above
  // so its metadata listener runs last and wins. Previews follow via the shared clock.
  useEffect(() => {
    if (!seekTo || appliedSeek.current === seekTo) return undefined;
    const video = ref.current;
    const apply = () => {
      if (Number.isFinite(video.duration) && seekTo.time + offset >= video.duration) {
        clock.current.stalled = true;
        clock.current.available = false;
        setError(true);
        setLoading(false);
        callbacks.current.onAvailability?.(feed.cameraId, false);
        callbacks.current.onPlayback?.();
        return;
      }
      video.currentTime = boundedTime(seekTo.time + offset, video.duration);
      clock.current.time = Math.max(0, video.currentTime - offset);
      appliedSeek.current = seekTo;
      callbacks.current.onPlayback?.();
    };
    if (video.readyState >= 1) {
      apply();
      return undefined;
    }
    video.addEventListener('loadedmetadata', apply, { once: true });
    return () => video.removeEventListener('loadedmetadata', apply);
  }, [seekTo, clock, offset, feed.cameraId, retry]);

  return (
    <div className="tile tile--main" aria-busy={loading || inferenceBuffering} style={{ viewTransitionName: 'main-player' }}>
      <div className="tile__media">
        <video ref={ref} aria-label={feed.label + ' video'} controls playsInline preload="auto" />
        <span className={`player-status ${loading || inferenceBuffering ? 'is-visible' : ''}`} role="status">
          {inferenceBuffering ? 'Starting inference...' : loading ? 'Loading match...' : ''}
        </span>
        {error && <div className="player-error" role="alert">
          <span>Video unavailable.</span>
          <button className="theme-toggle" onClick={() => setRetry((value) => value + 1)}>Retry</button>
        </div>}
      </div>
    </div>
  );
}
