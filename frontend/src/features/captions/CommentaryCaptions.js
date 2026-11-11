import { useEffect, useRef, useState } from 'react';
import { Captions } from 'lucide-react';
import { apiGet } from '../../shared/api/platformApi';
import { getVideoUrl, playbackIdentity } from '../watch/sources/feeds';
import { captionAt, readCaptionTrack } from './captions';
import useSourcePlayback, { SOURCE_LOAD_EVENT } from '../watch/playback/useSourcePlayback';
import { captionSessionEnded, liveCaptionStatus } from './liveCaptions';

const enabledPreference = () => { try { return localStorage.getItem('matchpulse-captions-enabled') === 'true'; } catch { return false; } };
export const CAPTION_READY_REFRESH_MS = 240000;
export const CAPTION_MISSING_RETRY_MS = 30000;
export const CAPTION_MIN_RETRY_MS = 5000;

export default function CommentaryCaptions({ feed, mediaRef, live, enabled: controlledEnabled, onToggle, connected = true, canControl = true }) {
  const [localEnabled, setEnabled] = useState(enabledPreference);
  const enabled = controlledEnabled ?? localEnabled;
  const isLive = live !== undefined;
  const [result, setResult] = useState(null);
  const captionTextRef = useRef(null);
  const { cameraId, sourceKey, sourceIdentity } = feed;
  const source = playbackIdentity(feed);
  const time = useSourcePlayback(mediaRef, `${getVideoUrl(feed)}\n${feed.cameraId}`, enabled, source);
  useEffect(() => {
    try {
      localStorage.setItem('matchpulse-captions-enabled', String(enabled));
    } catch {}
  }, [enabled]);
  useEffect(() => {
    if (isLive || !enabled || !sourceKey) return undefined;
    let cancelled = false;
    let timer;
    let expiryTimer;
    let loading = false;
    let generation = 0;
    let lastAttempt = -Infinity;
    let failures = 0;
    const video = mediaRef.current;
    setResult(null);
    const schedule = (delay) => {
      clearTimeout(timer);
      if (!cancelled) timer = setTimeout(() => refresh(true), Math.max(0, delay, lastAttempt + CAPTION_MIN_RETRY_MS - Date.now()));
    };
    const refresh = async (force) => {
      if (cancelled || loading) return;
      loading = true;
      lastAttempt = Date.now();
      const requestedGeneration = generation;
      try {
        const value = await apiGet('/api/captions', { match_id: cameraId, source_key: sourceKey, source_identity: sourceIdentity }, force);
        if (cancelled) return;
        if (generation !== requestedGeneration) { schedule(CAPTION_MIN_RETRY_MS); return; }
        failures = 0;
        const deadlines = [value.source_identity_valid_until, value.expires_at].filter(Number.isFinite);
        const validUntil = deadlines.length ? Math.min(...deadlines) * 1000 : Infinity;
        const track = validUntil > Date.now() ? readCaptionTrack(value, { cameraId, sourceKey, sourceIdentity }) : null;
        setResult({ source, track });
        clearTimeout(expiryTimer);
        if (track && Number.isFinite(validUntil)) expiryTimer = setTimeout(() => {
          if (!cancelled && generation === requestedGeneration) { setResult(null); schedule(0); }
        }, Math.max(0, validUntil - Date.now()));
        const suppliedRetry = Number.isFinite(value.retry_after_seconds) ? value.retry_after_seconds * 1000 : null;
        let delay = track ? CAPTION_READY_REFRESH_MS : suppliedRetry ?? (value.status === 'source_mismatch' ? CAPTION_READY_REFRESH_MS : CAPTION_MISSING_RETRY_MS);
        if (track && Number.isFinite(value.source_identity_revalidate_after_seconds)) delay = Math.min(delay, value.source_identity_revalidate_after_seconds * 1000);
        if (track && Number.isFinite(validUntil)) delay = Math.min(delay, validUntil - Date.now());
        schedule(Math.max(CAPTION_MIN_RETRY_MS, delay));
      } catch {
        if (!cancelled && generation === requestedGeneration) setResult({ source, track: null, unreachable: true });
        failures += 1;
        schedule(Math.min(CAPTION_READY_REFRESH_MS, CAPTION_MIN_RETRY_MS * 2 ** Math.min(failures - 1, 6)));
      } finally { loading = false; }
    };
    const reload = () => {
      generation += 1;
      clearTimeout(expiryTimer);
      setResult(null);
      schedule(Math.max(0, lastAttempt + CAPTION_MIN_RETRY_MS - Date.now()));
    };
    const nativeLoad = () => {
      // MainPlayer sends the explicit event before its own load. Do not double
      // request on that native loadstart; an independent native reload still
      // invalidates the track even when the resource URL is unchanged.
      if (!('matchpulseReadySource' in video) || video.matchpulseReadySource !== null) reload();
    };
    video?.addEventListener(SOURCE_LOAD_EVENT, reload);
    video?.addEventListener('loadstart', nativeLoad);
    refresh(false);
    return () => {
      cancelled = true; clearTimeout(timer); clearTimeout(expiryTimer);
      video?.removeEventListener(SOURCE_LOAD_EVENT, reload);
      video?.removeEventListener('loadstart', nativeLoad);
    };
  }, [enabled, source, cameraId, sourceKey, sourceIdentity, mediaRef, isLive]); // Feed object changes do not refetch tracks.
  const track = result?.source === source ? result.track : null;
  const liveMatches = live?.match_id === cameraId && live?.source_key === sourceKey
    && (!sourceIdentity || live?.source_identity === sourceIdentity);
  const text = enabled ? isLive ? liveMatches && connected && !captionSessionEnded(live) && time !== null ? live.text || '' : '' : captionAt(track, time) : '';
  useEffect(() => {
    const node = captionTextRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [text]);
  const covered = Number.isFinite(time) && track?.coverage_intervals?.some((interval) => interval.start_seconds <= time && time < interval.end_seconds);
  const status = isLive ? liveCaptionStatus(live, connected, canControl)
    : !feed.sourceKey ? 'Captions unavailable for this source.'
    : !result || result.source !== source ? 'Checking for a commentary track…'
      : result.unreachable ? 'The caption service is unavailable. Retrying shortly.'
        : !track ? 'Captions unavailable. No commentary track has been generated for this source.'
        : time === null ? 'Captions follow the player when the source is ready.'
          : !text ? covered ? 'No commentary at this moment.' : 'No caption track covers this part of the replay.' : '';
  const caption = <div ref={captionTextRef} tabIndex={text ? 0 : undefined} className={`caption-text ${isLive && live?.state === 'partial' ? 'caption-text--partial' : ''}`} aria-live="polite" aria-atomic="true">{text}</div>;
  return <section className="commentary-captions" aria-label="Commentary captions">
    <div className="caption-controls"><div className="caption-heading"><Captions size={21} aria-hidden="true" /><h2>Commentary captions</h2></div>
      <button className={`caption-toggle ${enabled ? 'is-on' : ''}`} aria-label={`Commentary captions ${enabled ? 'On' : 'Off'}`} aria-pressed={enabled}
          title={isLive ? 'Caption preference. The status below shows whether live transcription is available.' : undefined}
          onClick={() => { setEnabled(!enabled); onToggle?.(!enabled); }}><span className="caption-switch" aria-hidden="true"><i /></span><span>{enabled ? 'On' : 'Off'}</span></button>
    </div>
    {enabled && <>
      <div className="caption-panel">{caption}{!text && status && <p className="caption-status" role="status">{status}</p>}</div>
      {isLive && <p className="caption-provenance">Automatic captions by <a href="https://elevenlabs.io/" target="_blank" rel="noreferrer">ElevenLabs</a>. Draft text may change. Not human reviewed.</p>}
      {!isLive && track && <p className="caption-provenance">Automatic captions by <a href="https://elevenlabs.io/" target="_blank" rel="noreferrer">ElevenLabs</a>.{track.provenance.reviewed === true ? ' Human reviewed.' : ' Not human reviewed.'}</p>}
    </>}
  </section>;
}
