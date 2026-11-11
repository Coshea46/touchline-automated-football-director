import { useCallback, useEffect, useRef } from 'react';
import { getVideoUrl, sourceOffset } from '../sources/feeds';
import { validRecommendation } from '../session/replayProtocol';

export default function usePlayerHandoff({ focusedId, setFocusedId, clock, session, send, feedsRef,
  focusedRef, autoRef, holdRef, pendingSelection, pendingAck, transition, mainVideo, setAnnouncement, reportPlayback }) {
  const headingRef = useRef(null);
  const previousId = useRef(focusedId);
  const switchSource = useRef('user');
  const players = useRef(new Map());
  const registerPlayer = useCallback((matchId, video) => {
    if (video) players.current.set(matchId, video);
    else players.current.delete(matchId);
  }, []);
  const acknowledge = useCallback((recommendation) => {
    const current = session.current;
    if (!current || current.controls_enabled === false || recommendation.session_id !== current.session_id
      || recommendation.model_version !== current.model_version
      || recommendation.clock_epoch !== current.playback.clock_epoch
      || clock.current.stalled || clock.current.available === false) return;
    send.current({ type: 'ack', session_id: current.session_id, sequence: recommendation.sequence,
      clock_epoch: recommendation.clock_epoch, match_id: recommendation.selected_match_id,
      received_at: Date.now() / 1000 });
  }, [clock, send, session]);

  const finishRecommendation = useCallback(() => {
    const recommendation = pendingAck.current;
    const current = session.current;
    const feed = feedsRef.current.find((item) => item.id === focusedRef.current);
    if (!recommendation || !current || feed?.cameraId !== recommendation.selected_match_id
      || clock.current.stalled || clock.current.available === false) return;
    clock.current.sample?.();
    pendingAck.current = null;
    if (!autoRef.current || Date.now() < holdRef.current || !validRecommendation(recommendation,
      { ...current, sequence: recommendation.sequence - 1 }, clock.current)) return;
    acknowledge(recommendation);
    if (recommendation.announce !== false) {
      setAnnouncement(`Switched to ${feed.label}`);
    }
  }, [acknowledge, autoRef, clock, feedsRef, focusedRef, holdRef, pendingAck, session, setAnnouncement]);

  useEffect(() => {
    finishRecommendation();
    reportPlayback();
  }, [focusedId, finishRecommendation, reportPlayback]);

  useEffect(() => {
    // Only the user's own picks move keyboard focus; automatic switches never steal it.
    if (focusedId !== previousId.current && switchSource.current === 'user') headingRef.current?.focus();
    previousId.current = focusedId;
  }, [focusedId]);

  const switchTo = (id, source = 'user', recommendation = null) => {
    const priorAck = pendingAck.current;
    transition.current += 1;
    pendingSelection.current = null;
    pendingAck.current = null;
    if (id === focusedRef.current) {
      if (recommendation) {
        pendingAck.current = { ...recommendation, announce: priorAck?.selected_match_id === recommendation.selected_match_id
          ? priorAck.announce : false };
        finishRecommendation();
      }
      return;
    }
    pendingSelection.current = { id, source };
    clock.current.sample?.();
    if (recommendation && (!autoRef.current || Date.now() < holdRef.current
      || !validRecommendation(recommendation, { ...session.current, sequence: recommendation.sequence - 1 }, clock.current))) {
      pendingSelection.current = null;
      return;
    }
    if (recommendation) {
      const feed = feedsRef.current.find((item) => item.id === id);
      const video = feed && players.current.get(feed.cameraId);
      const actualTime = video ? Math.max(0, video.currentTime - sourceOffset(feed)) : NaN;
      // Never replace visible football with an automatic loading screen.
      // An unready proposal is discarded; a future fresh prediction can retry.
      if (!video || video.readyState < 2 || video.seeking
        || video.matchpulseReadySource !== `${getVideoUrl(feed)}\n${feed.cameraId}`
        || Math.abs(actualTime - clock.current.time) > 0.15
        || !validRecommendation(recommendation, { ...session.current, sequence: recommendation.sequence - 1 },
          { ...clock.current, time: actualTime, stalled: false, available: true })) {
        pendingSelection.current = null;
        return;
      }
    }
    switchSource.current = source;
    // Promote the already-decoding destination. Silence the old player before
    // its focus changes, without changing the viewer's mute preference.
    if (mainVideo.current) {
      mainVideo.current.matchpulseAudioOwned = false;
      mainVideo.current.muted = true;
    }
    delete clock.current.sample;
    clock.current.stalled = true;
    focusedRef.current = id;
    pendingSelection.current = null;
    pendingAck.current = recommendation ? { ...recommendation, announce: true } : null;
    setFocusedId(id);
  };

  return { headingRef, registerPlayer, finishRecommendation, switchTo };
}
