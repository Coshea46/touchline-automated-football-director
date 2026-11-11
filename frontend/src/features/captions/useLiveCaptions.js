import { useCallback, useEffect, useRef, useState } from 'react';
import { LIVE_CAPTION_MODEL, readLiveCaption } from './liveCaptions';

export default function useLiveCaptions({ session, feeds, focused, clock, media, send }) {
  // The session owns the default and remembers the viewer's requested state.
  const [enabled, setEnabled] = useState(true);
  const [value, setValue] = useState({ status: 'unavailable', available: false, model: LIVE_CAPTION_MODEL, text: '' });
  const valueRef = useRef(value);
  const desired = useRef(enabled);
  const minimum = useRef(0);
  const activeGeneration = useRef(-1);
  const lastPlayback = useRef(null);
  const pendingControl = useRef(null);
  const observerChoice = useRef(null);
  const wasController = useRef(false);
  const update = useCallback((next) => { valueRef.current = next; setValue(next); }, []);
  const control = useCallback((nextEnabled) => {
    desired.current = nextEnabled; setEnabled(nextEnabled);
    const current = session.current;
    if (!nextEnabled) {
      if (current?.controls_enabled !== false) minimum.current = Math.max(minimum.current, activeGeneration.current + 1);
      update({ ...valueRef.current, text: '', status: 'off' });
    }
    else if (current?.controls_enabled !== false) update({ ...valueRef.current, text: '', status: 'connecting' });
    if (current?.controls_enabled === false) { observerChoice.current = nextEnabled; return; }
    pendingControl.current = { session_id: current?.session_id || null, enabled: nextEnabled, sent: false };
    if (current?.captions) pendingControl.current.sent = send.current({ type: 'captions', session_id: current.session_id, enabled: nextEnabled });
  }, [session, send, update]);
  const onSession = useCallback((next) => {
    const previous = valueRef.current;
    const context = next.captions;
    const controlling = next.controls_enabled !== false;
    if (previous.session_id !== next.session_id) {
      minimum.current = 0; activeGeneration.current = -1; lastPlayback.current = null;
      observerChoice.current = null;
      if (pendingControl.current?.session_id && pendingControl.current.session_id !== next.session_id) pendingControl.current = null;
    }
    if (controlling && !wasController.current) observerChoice.current = null;
    const serverEnabled = typeof context?.user_enabled === 'boolean' ? context.user_enabled
      : typeof context?.enabled === 'boolean' ? context.enabled : true;
    let nextEnabled = serverEnabled;
    if (!controlling) {
      pendingControl.current = null;
      nextEnabled = observerChoice.current ?? serverEnabled;
    } else if (pendingControl.current) {
      const pending = pendingControl.current;
      pending.session_id = next.session_id;
      if (serverEnabled === pending.enabled) pendingControl.current = null;
      else {
        nextEnabled = pending.enabled;
        if (!pending.sent && context) pending.sent = send.current({ type: 'captions', session_id: next.session_id, enabled: pending.enabled });
      }
    }
    desired.current = nextEnabled; setEnabled(nextEnabled);
    wasController.current = controlling;
    const same = context && previous.session_id === next.session_id && previous.generation === context.generation
      && previous.match_id === context.match_id && previous.clock_epoch === context.clock_epoch;
    update({ ...previous, ...context, session_id: next.session_id, model: context?.model || LIVE_CAPTION_MODEL,
      status: context?.status || 'unavailable', available: context?.available === true,
      text: same && context.enabled && desired.current && ['listening', 'partial', 'committed', 'live', 'streaming', 'running'].includes(context.status) ? previous.text : '',
      source_key: same ? previous.source_key : undefined, source_identity: same ? previous.source_identity : undefined,
      state: same ? previous.state : null, receivedLocal: same ? previous.receivedLocal : null });
    const feed = feeds.current.find((item) => item.id === focused.current);
    if (context?.enabled && context.generation >= minimum.current && context.match_id === feed?.cameraId
      && context.clock_epoch === next.playback.clock_epoch && !clock.current.stalled && clock.current.available !== false
      && clock.current.playing && ['connecting', 'listening', 'partial', 'committed'].includes(context.status)) activeGeneration.current = context.generation;
  }, [clock, feeds, focused, send, update]);
  const onPlayback = useCallback(() => {
    const current = session.current;
    const feed = feeds.current.find((item) => item.id === focused.current);
    if (!current || !feed) return;
    const blocked = clock.current.stalled || clock.current.available === false || !clock.current.playing || clock.current.ended;
    const signature = `${current.session_id}:${current.playback.clock_epoch}:${feed.cameraId}:${feed.sourceIdentity || ''}`;
    const prior = lastPlayback.current;
    if (prior && (signature !== prior.signature || (blocked && !prior.blocked))) {
      minimum.current = Math.max(minimum.current, activeGeneration.current + 1);
      update({ ...valueRef.current, text: '', state: null, status: clock.current.available === false ? 'unavailable'
        : clock.current.stalled ? 'stalled' : 'paused' });
    }
    if (!blocked && current.captions?.enabled && current.captions.generation >= minimum.current
      && current.captions.match_id === feed.cameraId && current.captions.clock_epoch === current.playback.clock_epoch
      && ['connecting', 'listening', 'partial', 'committed'].includes(current.captions.status)) activeGeneration.current = current.captions.generation;
    lastPlayback.current = { signature, blocked };
  }, [clock, feeds, focused, session, update]);
  const onMessage = useCallback((message) => {
    if (!desired.current) return false;
    const current = session.current;
    const feed = feeds.current.find((item) => item.id === focused.current);
    const accepted = readLiveCaption(message, current, feed, { ...clock.current, sourceTime: media.current?.currentTime }, minimum.current);
    if (!accepted) return false;
    if (accepted.state === 'partial' || accepted.state === 'committed') activeGeneration.current = accepted.generation;
    update({ ...valueRef.current, ...accepted });
    return true;
  }, [clock, feeds, focused, media, session, update]);
  const onConnection = useCallback((connected) => {
    if (connected) return;
    if (pendingControl.current) pendingControl.current.sent = false;
    update({ ...valueRef.current, text: '', state: null, status: 'offline' });
  }, [update]);
  useEffect(() => {
    if (!value.text) return undefined;
    const timer = setTimeout(() => update({ ...valueRef.current, text: '', state: null }), 10000);
    return () => clearTimeout(timer);
  }, [value.text, value.receivedLocal, update]);
  return { enabled, value, control, onSession, onMessage, onPlayback, onConnection };
}
