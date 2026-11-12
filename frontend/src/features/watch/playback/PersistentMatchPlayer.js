import { useLayoutEffect, useRef, useState } from 'react';
import { sourceOffset } from '../sources/feeds';
import { SOURCE_LOAD_EVENT, SOURCE_READY_EVENT } from './useSourcePlayback';
import ReplayScore from './ReplayScore';
import useMediaAuthorization from './useMediaAuthorization';

const ALIGNMENT_SECONDS = .15;
const WARM_SEEK_SECONDS = .5;
const AUTHORIZATION_ALIGNMENT_SECONDS = .001;
const MAX_AUTHORIZATION_SEEKS = 3;

// Each registered source owns one native element for its whole lifetime. Focus
// changes only presentation/clock ownership; a warm aligned source never reloads.
export default function PersistentMatchPlayer({ feed, focused, focusedRef, clock, seekTo, onPlayback,
  onAvailability, mediaRef, inferenceBuffering = false, audioAllowed = true, previewsPaused = false,
  watching = true, onClick, score, scoreKind = 'model', monitoring = false, style, registerPlayer }) {
  const ref = useRef(null);
  const props = useRef(null);
  const actions = useRef(null);
  const appliedSeek = useRef(null);
  const appliedRetry = useRef(0);
  const runtime = useRef({ ready: false, failed: false, metadata: false, seekTarget: null,
    pendingExternal: null, externalApplied: false, needsAlignment: true });
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const authorization = useMediaAuthorization(feed, clock);
  const source = authorization.url;
  const identity = authorization.identity;
  const recoverAuthorization = authorization.recover;
  const offset = sourceOffset(feed);
  const cameraId = feed.cameraId;
  props.current = { feed, focused, focusedRef, clock, onPlayback, onAvailability, mediaRef,
    inferenceBuffering, audioAllowed, previewsPaused, watching, source, identity };

  useLayoutEffect(() => {
    const video = ref.current;
    const replayClock = clock.current;
    const state = runtime.current;
    state.authorizationReload = Boolean(state.identity === identity && state.source
      && (state.source !== source || state.authorizationReload));
    state.authorizationSeeks = 0;
    state.identity = identity;
    state.source = source;
    let disposed = false;
    let requestedPlay = false;
    const active = () => {
      const current = props.current;
      return current.focused && (!current.focusedRef || current.focusedRef.current === current.feed.id);
    };
    const silence = ({ preserveOwnership = false } = {}) => {
      video.defaultMuted = true;
      if (!preserveOwnership) video.matchpulseAudioOwned = false;
      if (!video.muted) video.muted = true;
    };
    const audio = () => {
      if (!active()) { silence(); return; }
      const current = props.current;
      const muted = !current.audioAllowed || video.matchpulseAudioOwned === false || clock.current.muted;
      if (video.muted !== Boolean(muted)) video.muted = Boolean(muted);
      const volume = Number.isFinite(clock.current.volume) ? clock.current.volume : 1;
      if (video.volume !== volume) video.volume = volume;
    };
    const sample = () => {
      if (disposed || !active() || !state.ready || state.failed || video.seeking || state.seekTarget !== null) return;
      clock.current.time = Math.max(0, video.currentTime - offset);
      clock.current.rate = video.playbackRate;
      if (props.current.audioAllowed && video.matchpulseAudioOwned !== false) clock.current.muted = video.muted;
      clock.current.volume = video.volume;
    };
    const report = () => {
      if (!active() || disposed) return;
      sample();
      props.current.onPlayback?.();
    };
    const unavailable = () => {
      if (disposed) return;
      state.failed = true;
      state.ready = false;
      state.seekTarget = null;
      video.matchpulseReadySource = null;
      video.matchpulseReadyIdentity = null;
      silence({ preserveOwnership: active() && props.current.audioAllowed });
      setFailed(true); setLoading(false);
      props.current.onAvailability?.(cameraId, false);
      if (active()) {
        clock.current.available = false;
        clock.current.stalled = true;
        props.current.onPlayback?.();
      }
    };
    const play = () => {
      if (disposed || requestedPlay || !video.paused) return;
      requestedPlay = true;
      let attempt;
      try { attempt = video.play(); } catch { requestedPlay = false; unavailable(); return; }
      Promise.resolve(attempt).catch((reason) => {
        if (disposed || reason?.name === 'AbortError') return;
        if (reason?.name === 'NotAllowedError') {
          video.muted = true;
          if (active()) clock.current.muted = true;
          return video.play()?.catch(() => {
            if (active() && !disposed) { clock.current.playing = false; report(); }
          });
        }
        unavailable();
        return undefined;
      }).finally(() => { requestedPlay = false; });
    };
    const pause = () => { if (!video.paused) video.pause(); };
    const completed = () => {
      if (disposed || state.failed || !state.metadata || video.readyState < 2 || video.seeking) return false;
      state.seekTarget = null;
      state.pendingExternal = null;
      state.externalApplied = false;
      state.needsAlignment = false;
      state.authorizationReload = false;
      state.ready = true;
      video.matchpulseReadySource = `${source}\n${cameraId}`;
      video.matchpulseReadyIdentity = identity;
      setLoading(false); setFailed(false);
      if (active()) {
        clock.current.sample = sample;
        clock.current.available = true;
        clock.current.stalled = false;
        clock.current.ended = false;
        sample();
      }
      video.dispatchEvent(new Event(SOURCE_READY_EVENT));
      props.current.onAvailability?.(cameraId, true);
      if (active()) props.current.onPlayback?.();
      return true;
    };
    const seek = (target) => {
      if (video.seeking || state.seekTarget !== null) return false;
      if (state.authorizationReload && active()) {
        // A decoder may expose only coarse seek positions. Fail normally rather
        // than loop or report a fabricated clock ahead of its decoded source.
        if (state.authorizationSeeks >= MAX_AUTHORIZATION_SEEKS) { unavailable(); pause(); return false; }
        state.authorizationSeeks += 1;
      }
      state.ready = false;
      state.needsAlignment = true;
      state.seekTarget = target;
      if (state.pendingExternal !== null) state.externalApplied = true;
      video.matchpulseReadySource = null;
      video.matchpulseReadyIdentity = null;
      video.dispatchEvent(new Event(SOURCE_LOAD_EVENT));
      if (active()) {
        setLoading(true);
        clock.current.stalled = true;
        props.current.onPlayback?.();
      }
      video.currentTime = target;
      return true;
    };
    const sync = ({ promotion = false, external = null } = {}) => {
      if (disposed || state.failed) return;
      const current = props.current;
      const isActive = active();
      const externalTarget = external ?? state.pendingExternal;
      if (!isActive) clock.current.sample?.();
      else if (!promotion && externalTarget === null) sample();
      audio();
      if (!state.metadata || video.readyState < 1) return;
      const target = Math.max(0, (externalTarget ?? clock.current.time ?? 0) + offset);
      if (Number.isFinite(video.duration) && target >= video.duration) { unavailable(); pause(); return; }
      if (video.seeking || state.seekTarget !== null) return;
      const difference = target - video.currentTime;
      // Same-source renewal must not use the 150ms warm-handoff tolerance:
      // the backend treats backward movement over 1ms as an actual user seek.
      const threshold = state.authorizationReload && isActive ? AUTHORIZATION_ALIGNMENT_SECONDS
        : externalTarget !== null && !state.externalApplied ? .001
        : isActive || promotion || externalTarget !== null ? ALIGNMENT_SECONDS : WARM_SEEK_SECONDS;
      if (Math.abs(difference) > threshold) {
        seek(target);
        return;
      }
      const baseRate = clock.current.rate || 1;
      const desiredRate = isActive || Math.abs(difference) < .04 ? baseRate
        : baseRate * Math.max(.95, Math.min(1.05, 1 + difference * .2));
      if (video.playbackRate !== desiredRate) video.playbackRate = desiredRate;
      if (!state.ready || promotion || externalTarget !== null) completed();
      const shouldPlay = clock.current.playing && !current.inferenceBuffering
        && (isActive || (!current.previewsPaused && current.watching && !clock.current.stalled));
      if (shouldPlay) play(); else pause();
    };
    const metadata = () => {
      if (disposed) return;
      if (video.currentSrc && video.currentSrc !== source) return;
      state.metadata = true;
      sync();
    };
    const decoded = () => {
      if (disposed || video.seeking || video.readyState < 2) return;
      if (video.currentSrc && video.currentSrc !== source) return;
      state.metadata = true;
      state.seekTarget = null;
      // A resumed active decoder advances the actual clock. Only a pending
      // source/promotion/explicit alignment may target an earlier shared time.
      if (active() && !state.needsAlignment && state.pendingExternal === null) completed();
      sync();
    };
    const seeking = () => {
      if (disposed) return;
      const userSeek = state.seekTarget === null && !state.needsAlignment;
      state.ready = false;
      video.matchpulseReadySource = null;
      video.matchpulseReadyIdentity = null;
      if (active()) {
        if (userSeek) clock.current.time = Math.max(0, video.currentTime - offset);
        clock.current.stalled = true;
        props.current.onPlayback?.();
      }
    };
    const playing = () => {
      audio();
      if (!active()) return;
      if (props.current.inferenceBuffering) {
        clock.current.inferencePlaybackIntent = true;
        clock.current.playing = false;
        pause(); report();
      } else if (state.ready) {
        clock.current.playing = true;
        clock.current.stalled = false;
        report();
      }
    };
    const paused = () => {
      if (active() && state.ready && !props.current.inferenceBuffering) {
        clock.current.playing = false;
        report();
      }
    };
    const stalled = () => {
      state.ready = false;
      video.matchpulseReadySource = null;
      video.matchpulseReadyIdentity = null;
      if (active()) { clock.current.stalled = true; report(); }
    };
    const ended = () => {
      state.ready = false;
      video.matchpulseReadySource = null;
      video.matchpulseReadyIdentity = null;
      silence();
      if (active()) {
        clock.current.playing = false; clock.current.ended = true;
        props.current.onPlayback?.();
      }
    };
    const volume = () => { if (!active()) silence(); else sample(); };
    const rate = () => { if (active()) report(); };
    const mediaError = () => {
      if (!recoverAuthorization()) { unavailable(); return; }
      state.ready = false;
      video.matchpulseReadySource = null;
      video.matchpulseReadyIdentity = null;
      if (active()) { clock.current.stalled = true; report(); }
    };
    const listeners = { loadedmetadata: metadata, loadeddata: decoded, canplay: decoded, seeked: decoded,
      seeking, timeupdate: sample, ratechange: rate, volumechange: volume, play: playing, playing,
      pause: paused, waiting: stalled, stalled, ended, error: mediaError };
    Object.entries(listeners).forEach(([name, handler]) => video.addEventListener(name, handler));
    actions.current = { sync, sample, audio, silence, completed, pause };
    state.ready = false; state.failed = false; state.seekTarget = null; state.needsAlignment = true;
    video.matchpulseReadySource = null;
    video.matchpulseReadyIdentity = null;
    video.matchpulseSourceIdentity = identity;
    setLoading(true); setFailed(false);
    const changed = video.getAttribute('src') !== source;
    const retryRequested = retry !== appliedRetry.current;
    appliedRetry.current = retry;
    state.metadata = !changed && video.readyState >= 1;
    if (source && (changed || retryRequested)) video.dispatchEvent(new Event(SOURCE_LOAD_EVENT));
    if (!source) {
      state.metadata = false;
      silence();
      if (video.hasAttribute('src')) { video.removeAttribute('src'); video.load(); }
      if (active()) { clock.current.available = false; clock.current.stalled = true; }
    } else if (changed) video.src = source;
    else if (retryRequested) { state.metadata = false; video.load(); }
    else sync();
    const timer = setInterval(() => { if (!active()) sync(); }, 250);
    return () => {
      disposed = true;
      clearInterval(timer);
      if (replayClock.sample === sample) delete replayClock.sample;
      if (actions.current?.sample === sample) actions.current = null;
      Object.entries(listeners).forEach(([name, handler]) => video.removeEventListener(name, handler));
      silence({ preserveOwnership: props.current.identity === identity && props.current.source !== source });
      pause();
    };
  }, [source, identity, offset, cameraId, clock, retry, recoverAuthorization]);

  useLayoutEffect(() => {
    registerPlayer?.(cameraId, ref.current);
    return () => registerPlayer?.(cameraId, null);
  }, [cameraId, registerPlayer]);

  // Apply an explicit seek before focus/playback synchronization can sample the
  // outgoing native position (especially when starting a fresh visitor replay).
  useLayoutEffect(() => {
    if (!seekTo || appliedSeek.current === seekTo) return;
    appliedSeek.current = seekTo;
    const active = focused && (!focusedRef || focusedRef.current === feed.id);
    runtime.current.pendingExternal = seekTo.time;
    runtime.current.externalApplied = false;
    runtime.current.needsAlignment = true;
    runtime.current.ready = false;
    ref.current.matchpulseReadySource = null;
    ref.current.matchpulseReadyIdentity = null;
    ref.current.dispatchEvent(new Event(SOURCE_LOAD_EVENT));
    if (active) {
      clock.current.stalled = true;
      clock.current.available = false;
      setLoading(true);
      props.current.onPlayback?.();
    }
    actions.current?.sync({ external: seekTo.time });
  }, [seekTo, focused, focusedRef, feed.id, clock]);

  useLayoutEffect(() => {
    const video = ref.current;
    const replayClock = clock.current;
    const current = actions.current;
    if (!current) return undefined;
    if (focused && (!focusedRef || focusedRef.current === feed.id)) {
      if (mediaRef) mediaRef.current = video;
      clock.current.sample = current.sample;
      clock.current.stalled = true;
      runtime.current.needsAlignment = true;
      current.sync({ promotion: true });
      if (!runtime.current.ready) { setLoading(true); props.current.onPlayback?.(); }
    } else current.silence();
    return () => {
      current.silence({ preserveOwnership: props.current.identity === identity && props.current.source !== source });
      if (replayClock.sample === current.sample) delete replayClock.sample;
      if (mediaRef?.current === video) mediaRef.current = null;
    };
  }, [focused, focusedRef, feed.id, source, identity, offset, cameraId, mediaRef, clock]);

  useLayoutEffect(() => {
    const active = focused && (!focusedRef || focusedRef.current === feed.id);
    if (active) {
      if (inferenceBuffering) {
        if (!clock.current.inferencePlaybackHeld) {
          clock.current.inferencePlaybackIntent = clock.current.playing;
          clock.current.inferencePlaybackHeld = true;
        }
        clock.current.playing = false;
        actions.current?.pause();
      } else if (clock.current.inferencePlaybackHeld) {
        clock.current.playing = clock.current.inferencePlaybackIntent;
        delete clock.current.inferencePlaybackHeld;
        delete clock.current.inferencePlaybackIntent;
      }
      actions.current?.audio();
      actions.current?.sync();
      props.current.onPlayback?.();
    } else if (inferenceBuffering || previewsPaused || !watching || clock.current.playing === false) {
      // A handoff briefly sets the shared stalled flag before the incoming layout
      // completes. Do not pause/restart the outgoing warm decoder in that gap.
      actions.current?.pause();
    }
  }, [focused, focusedRef, feed.id, inferenceBuffering, audioAllowed, previewsPaused, watching, clock]);

  const select = (event) => {
    if (focused) return;
    if (event.type === 'keydown') {
      if (!['Enter', ' '].includes(event.key)) return;
      event.preventDefault();
    }
    onClick?.();
  };
  return <div className={`tile persistent-player ${focused ? 'tile--main' : 'tile--preview'}`}
    role={focused ? undefined : 'button'} tabIndex={focused ? undefined : 0}
    aria-label={focused ? undefined : feed.healthy === false ? `${feed.label} (monitoring unavailable)` : feed.label}
    aria-busy={loading || (focused && inferenceBuffering)} data-feed-id={feed.id}
    style={style} onClick={select} onKeyDown={select}>
    <div className="tile__media">
      <video ref={ref} aria-label={focused ? `${feed.label} video` : undefined} aria-hidden={focused ? undefined : true}
        tabIndex={focused ? undefined : -1} controls={focused} playsInline preload="auto" muted
        className={loading ? undefined : 'is-ready'} />
      {focused && <span className={`player-status ${loading || inferenceBuffering ? 'is-visible' : ''}`} role="status">
        {inferenceBuffering ? 'Starting inference...' : loading ? 'Loading match...' : ''}
      </span>}
      {failed && <div className={focused ? 'player-error' : 'preview-status'} role={focused ? 'alert' : 'status'}>
        <span>{focused ? 'Video unavailable.' : 'Match unavailable'}</span>
        <button type="button" className="theme-toggle" onKeyDown={(event) => event.stopPropagation()}
          onClick={(event) => { event.stopPropagation(); setRetry((value) => value + 1); }}>Retry</button>
      </div>}
    </div>
    {!focused && <span className="tile__label"><span className="tile__name">{feed.label}</span>
      <ReplayScore feed={feed} mediaRef={ref} source={source} />
      <span className="tile__details"><span className={monitoring && feed.healthy !== false ? 'is-monitoring' : ''}>
        {feed.healthy === false ? 'Monitoring unavailable' : 'Replay'}</span>
        {Number.isFinite(score) && <span className="relative-score" title={`Relative ${scoreKind} score; not a goal probability`}>
          {scoreKind === 'mock' ? 'Mock' : 'Score'} {score.toFixed(3)}</span>}
      </span></span>}
  </div>;
}
