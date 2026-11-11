import { useCallback, useEffect, useRef, useState } from 'react';
import { FEEDS } from '../sources/feeds';
import useBackendSocket from '../../../shared/api/useBackendSocket';
import { isUnavailableFallback, readSession, validRecommendation } from './replayProtocol';
import useLiveCaptions from '../../captions/useLiveCaptions';
import useAudioOwnership from '../audio/useAudioOwnership';
import usePlayerHandoff from '../playback/usePlayerHandoff';
import useDirectorControls from '../director/useDirectorControls';
import directorViewState from '../director/directorViewState';

export const MANUAL_HOLD_MS = 30000;

// The one Watch coordinator remains mounted while navigation only hides its view.
export default function useWatchReplay(activePage) {
  const [feeds, setFeeds] = useState(FEEDS);
  const [sessionState, setSessionState] = useState(null);
  const [backendError, setBackendError] = useState('');
  const [claimPending, setClaimPending] = useState(false);
  const [controlNotice, setControlNotice] = useState('');
  const [audioEnabled, setAudioEnabled] = useState(false);
  const [claimedAudio, setClaimedAudio] = useState(null);
  const [uiNow, setUiNow] = useState(Date.now());
  const [focusedId, setFocusedId] = useState(FEEDS[0].id);
  const [previewsPaused, setPreviewsPaused] = useState(false);
  const [autoSwitch, setAutoSwitch] = useState(true);
  const [holdUntil, setHoldUntil] = useState(0);
  const [seekTo, setSeekTo] = useState(null);
  const [announcement, setAnnouncement] = useState('');
  const clock = useRef({ time: 0, playing: true, rate: 1, muted: false, volume: 1, stalled: true, available: true });
  const send = useRef(() => false);
  const session = useRef(null);
  const pendingClaim = useRef(null);
  const roleConfirmed = useRef(false);
  const feedsRef = useRef(feeds);
  const focusedRef = useRef(focusedId);
  const autoRef = useRef(autoSwitch);
  const holdRef = useRef(holdUntil);
  const dirtyControls = useRef({ auto: false, select: false });
  const unavailable = useRef(new Set());
  const latestRecommendation = useRef(null);
  const transition = useRef(0);
  const pendingSelection = useRef(null);
  const pendingAck = useRef(null);
  const mainVideo = useRef(null);
  const audio = useAudioOwnership({ media: mainVideo, clock, sourceId: focusedId, enabled: audioEnabled });
  const requestOwnedAudio = audio.requestAudio;
  const synced = useRef(false);
  const serverSkew = useRef(0);
  const { director, setDirector, pendingMode, setPendingMode, modeNotice, setModeNotice,
    directorRef, modePreference, pendingModeRef, modeRequestSession, updateDirector, requestMode } =
    useDirectorControls({ session, send, pendingSelection, pendingAck, transition });
  const focused = feeds.find((feed) => feed.id === focusedId) || feeds[0];
  feedsRef.current = feeds;
  focusedRef.current = focused.id;
  autoRef.current = autoSwitch;
  holdRef.current = holdUntil;
  const captions = useLiveCaptions({ session, feeds: feedsRef, focused: focusedRef, clock, media: mainVideo, send });
  const captionPlayback = captions.onPlayback;
  const captionConnection = captions.onConnection;

  const reportPlayback = useCallback(() => {
    clock.current.sample?.();
    const current = session.current;
    const feed = feedsRef.current.find((item) => item.id === focusedRef.current);
    if (!current || !feed) return;
    captionPlayback();
    if (current.controls_enabled === false) return;
    send.current({
      type: 'playback', session_id: current.session_id, match_id: feed.cameraId,
      position: clock.current.time, playing: clock.current.playing, rate: clock.current.rate,
      stalled: Boolean(clock.current.stalled), available: clock.current.available !== false,
      ended: Boolean(clock.current.ended),
    });
  }, [captionPlayback]);

  useEffect(() => {
    const timer = setInterval(reportPlayback, 250);
    return () => clearInterval(timer);
  }, [reportPlayback]);

  useEffect(() => {
    const timer = setInterval(() => setUiNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!announcement) return undefined;
    const timer = setTimeout(() => setAnnouncement(''), 4500);
    return () => clearTimeout(timer);
  }, [announcement]);

  useEffect(() => {
    if (!claimPending) return undefined;
    const timer = setTimeout(() => {
      pendingClaim.current = null;
      setClaimPending(false);
      setControlNotice('Control was not confirmed. Try again.');
    }, 8000);
    return () => clearTimeout(timer);
  }, [claimPending]);

  useEffect(() => {
    if (!claimedAudio) return;
    if (session.current?.session_id === claimedAudio && session.current.controls_enabled === true && audioEnabled) {
      setClaimedAudio(null);
      void requestOwnedAudio();
    } else if (session.current?.session_id !== claimedAudio || session.current?.controls_enabled === false) {
      setClaimedAudio(null);
    }
  }, [claimedAudio, audioEnabled, requestOwnedAudio]);

  const claimControl = () => {
    const current = session.current;
    if (pendingClaim.current || connection !== 'connected' || !current || current.controls_enabled !== false
      || current.can_claim_control !== true) return;
    pendingClaim.current = { session_id: current.session_id };
    setClaimPending(true);
    setControlNotice('');
    if (!send.current({ type: 'claim_control', session_id: current.session_id })) {
      pendingClaim.current = null;
      setClaimPending(false);
      setControlNotice('The request could not be sent. Reconnect and try again.');
    }
  };

  const { headingRef, registerPlayer, finishRecommendation, switchTo } = usePlayerHandoff({
    focusedId, setFocusedId, clock, session, send, feedsRef, focusedRef, autoRef, holdRef,
    pendingSelection, pendingAck, transition, mainVideo, setAnnouncement, reportPlayback,
  });

  useEffect(() => {
    if (!holdUntil) return undefined;
    const timer = setTimeout(() => { holdRef.current = 0; setHoldUntil(0); }, Math.max(0, holdUntil - Date.now()));
    return () => clearTimeout(timer);
  }, [holdUntil]);

  // A manual pick wins over the backend for MANUAL_HOLD_MS.
  const pick = (id) => {
    const feed = feedsRef.current.find((item) => item.id === id);
    if (!feed) return;
    holdRef.current = Date.now() + MANUAL_HOLD_MS;
    setHoldUntil(holdRef.current);
    const current = session.current;
    dirtyControls.current.select = !current || current.controls_enabled === false || !send.current({
      type: 'select', session_id: current.session_id, match_id: feed.cameraId,
      manual_hold_seconds: MANUAL_HOLD_MS / 1000,
    });
    switchTo(id);
  };

  const connection = useBackendSocket((message) => {
    if (message.type === 'session') {
      const next = readSession(message, session.current);
      if (!next) return;
      if (session.current && (session.current.session_id !== next.session_id
        || session.current.playback.clock_epoch !== next.playback.clock_epoch)) {
        if (session.current.session_id !== next.session_id || pendingSelection.current?.source === 'auto') {
          transition.current += 1;
          pendingSelection.current = null;
        }
        pendingAck.current = null;
      }
      if (session.current && session.current.session_id !== next.session_id) {
        directorRef.current = null;
        setDirector(null);
      }
      const firstSession = !session.current;
      const claimed = pendingClaim.current?.session_id === next.session_id && next.controls_enabled === true;
      const lostControl = session.current?.controls_enabled === true && next.controls_enabled === false;
      if (next.controls_enabled === false) audio.releaseAudio();
      if (lostControl || claimed) {
        transition.current += 1;
        pendingSelection.current = null;
        pendingAck.current = null;
        latestRecommendation.current = null;
        dirtyControls.current = { auto: false, select: false };
        pendingModeRef.current = null;
        setPendingMode(null);
        setModeNotice('');
      }
      if (pendingClaim.current && (next.session_id !== pendingClaim.current.session_id || claimed)) {
        pendingClaim.current = null;
        setClaimPending(false);
        setControlNotice('');
      }
      if (claimed) {
        // Adopt the authoritative settings. Observer-local picks/preferences must
        // not replay stale Auto/mode commands when control changes hands.
        modeRequestSession.current = next.session_id;
        setClaimedAudio(next.session_id);
      }
      roleConfirmed.current = true;
      setAudioEnabled(next.controls_enabled === true);
      const freshStartCapability = next.mode === 'disabled' && typeof next.auto_can_start === 'boolean'
        && (firstSession || session.current.session_id !== next.session_id);
      if (freshStartCapability) dirtyControls.current.auto = false;
      next.feeds.forEach((feed) => {
        const previous = feedsRef.current.find((item) => item.cameraId === feed.cameraId);
        if (previous?.url !== feed.url) unavailable.current.delete(feed.cameraId);
      });
      session.current = next;
      captions.onSession(next);
      if (pendingAck.current && !next.feeds.some((feed) => feed.cameraId === pendingAck.current.selected_match_id && feed.healthy)) {
        pendingAck.current = null;
      }
      serverSkew.current = next.server_time - Date.now() / 1000;
      feedsRef.current = next.feeds;
      setFeeds(next.feeds);
      setSessionState(next);
      setBackendError('');
      const nextDirector = updateDirector(next.director, next.feeds);
      if (claimed && nextDirector) modePreference.current = nextDirector.mode;
      if (nextDirector && modeRequestSession.current !== next.session_id && next.controls_enabled !== false) {
        modeRequestSession.current = next.session_id;
        if (modePreference.current && modePreference.current !== nextDirector.mode) requestMode(modePreference.current);
      }
      // Join the shared replay once. Reconnection must not rewind a viewer who
      // has continued to play while the backend was unavailable.
      if (!synced.current) {
        synced.current = true;
        if (clock.current.time <= 0.05 && next.playback.position > 0) {
          setSeekTo({ time: next.playback.position });
        }
      }
      const preservedId = pendingSelection.current?.id ?? focusedRef.current;
      const preserved = next.feeds.find((feed) => feed.id === preservedId);
      const selected = next.feeds.find((feed) => feed.cameraId === next.current_match_id && feed.healthy);
      const target = (firstSession && !dirtyControls.current.select ? selected : preserved)
        || preserved || selected || next.feeds.find((feed) => feed.healthy) || next.feeds[0];
      if (target.id !== pendingSelection.current?.id) switchTo(target.id, 'auto');
      if (dirtyControls.current.auto && next.controls_enabled !== false) {
        dirtyControls.current.auto = !send.current({ type: 'auto', session_id: next.session_id, enabled: autoRef.current });
      } else if (typeof next.auto_requested === 'boolean' || typeof next.auto_enabled === 'boolean') {
        const requested = typeof next.auto_requested === 'boolean' ? next.auto_requested : next.auto_enabled;
        autoRef.current = requested;
        setAutoSwitch(requested);
      }
      if (dirtyControls.current.select && next.controls_enabled !== false) {
        dirtyControls.current.select = !send.current({ type: 'select', session_id: next.session_id,
          match_id: target.cameraId, manual_hold_seconds: Math.max(0, (holdRef.current - Date.now()) / 1000) });
      } else if (Number.isFinite(next.manual_hold_until)) {
        holdRef.current = next.manual_hold_until * 1000 > Date.now() ? next.manual_hold_until * 1000 : 0;
        setHoldUntil(holdRef.current);
      }
      reportPlayback();
    } else if (message.type === 'caption') {
      clock.current.sample?.();
      if (captions.onMessage(message)) session.current = { ...session.current, sequence: message.sequence };
    } else if (message.type === 'recommendation') {
      clock.current.sample?.();
      // Scores can arrive while the newly selected source is buffering. Consume
      // their identity/order without applying another switch, and retain only
      // the newest matching ACK until the actual target is ready.
      if (!validRecommendation(message, session.current, { ...clock.current, stalled: false })) return;
      session.current = { ...session.current, sequence: message.sequence };
      updateDirector(message.director, session.current.feeds);
      latestRecommendation.current = message;
      if (!autoRef.current || Date.now() < holdRef.current || clock.current.ended) return;
      const target = session.current.feeds.find((feed) => feed.cameraId === message.selected_match_id);
      if (unavailable.current.has(target.cameraId)) return;
      if (clock.current.stalled && !isUnavailableFallback(message, clock.current)) {
        pendingAck.current = pendingAck.current && target.id === focusedRef.current
          ? { ...message, announce: pendingAck.current.announce } : null;
        return;
      }
      switchTo(target.id, 'auto', message);
    } else if (message.type === 'error' && message.code === 'viewer_in_use') {
      if (session.current) session.current.controls_enabled = false;
      setSessionState((previous) => previous ? { ...previous, controls_enabled: false } : previous);
      setAudioEnabled(false);
      audio.releaseAudio();
      setBackendError('Viewing only');
    } else if (message.type === 'error' && pendingClaim.current
      && ['session_mismatch', 'viewer_not_registered', 'session_closed', 'unknown_message'].includes(message.code)) {
      pendingClaim.current = null;
      setClaimPending(false);
      setControlNotice('This tab could not take control. Reconnect and try again.');
    } else if (message.type === 'error' && message.code === 'invalid_director_mode') {
      pendingModeRef.current = null;
      setPendingMode(null);
      setModeNotice('That mode could not be applied. The current settings are unchanged.');
    }
  }, undefined, send);

  useEffect(() => {
    if (connection !== 'connected') {
      modeRequestSession.current = null;
      roleConfirmed.current = false;
      if (pendingClaim.current) {
        pendingClaim.current = null;
        setClaimPending(false);
        setControlNotice('The replay connection was lost. Reconnect before taking control.');
      }
      setClaimedAudio(null);
      setAudioEnabled(connection === 'offline' && session.current?.controls_enabled !== false);
    } else setAudioEnabled(roleConfirmed.current && session.current?.controls_enabled === true);
    captionConnection(connection === 'connected');
  }, [connection, captionConnection, modeRequestSession]);

  const toggleAuto = () => {
    if (!autoToggleable) return;
    const enabled = autoCanStart ? true : !autoRef.current;
    if (!enabled) {
      transition.current += 1;
      pendingSelection.current = null;
      pendingAck.current = null;
    }
    autoRef.current = enabled;
    setAutoSwitch(enabled);
    const current = session.current;
    dirtyControls.current.auto = !current || current.controls_enabled === false
      || !send.current({ type: 'auto', session_id: current.session_id, enabled });
  };

  const onPlayerKey = (event) => {
    if (activePage !== 'watch') return;
    if (event.altKey || event.ctrlKey || event.metaKey || event.target.closest('video')) return;
    // Single-key shortcuts are active only while focus is in the player area.
    const index = Number(event.key) - 1;
    if (Number.isInteger(index) && feeds[index]) pick(feeds[index].id);
    if (event.key === 'Escape') document.querySelector('.filmstrip .tile:not([hidden])[role="button"]')?.focus();
  };

  const view = directorViewState({ connection, sessionState, backendError, autoSwitch, holdUntil,
    director, uiNow, serverSkew: serverSkew.current, feeds });
  const { autoCanStart, autoToggleable } = view;

  return { feeds, focused, focusedRef, headingRef, registerPlayer, clock, seekTo, reportPlayback, mainVideo,
    audio, audioEnabled, claimPending, claimControl, session, sessionState, director, pendingMode, modeNotice,
    requestMode, controlNotice, holdUntil, connection, captions, autoSwitch, previewsPaused, setPreviewsPaused,
    pick, unavailable, finishRecommendation, pendingAck, toggleAuto, onPlayerKey, announcement, view };
}
