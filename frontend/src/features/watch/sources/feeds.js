import matchCatalogue from '../../../data/matches.json';

const DEFAULT_BASE = 'https://match-switcher-poc-videos.s3.eu-west-2.amazonaws.com';
const S3_BASE_URL = (process.env.REACT_APP_S3_BASE_URL || DEFAULT_BASE).replace(/\/$/, '');
const PREVIEW_BASE_URL = (process.env.REACT_APP_PREVIEW_BASE_URL || S3_BASE_URL).replace(/\/$/, '');
const TEST_VIDEO_KEY = process.env.REACT_APP_TEST_VIDEO_KEY;
const PREVIEW_KEY = process.env.REACT_APP_TEST_PREVIEW_KEY;

// Each match is match-<n>.mp4 at the bucket root. Its lightweight preview
// (used by the small tiles) sits next to it as match-<n>-preview-v1.mp4.
// REACT_APP_TEST_VIDEO_KEY / REACT_APP_TEST_PREVIEW_KEY force every feed to one video, for testing.
// The backend names feeds "cam-1" ... "cam-7", in the order of VIDEO_KEYS in backend/main.py.
const previewKeyFor = (key) => key.replace(/\.mp4$/i, '-preview-v1.mp4');

export const FEEDS = Array.from({ length: 7 }, (_, i) => {
  const mainKey = TEST_VIDEO_KEY || `match-${i + 1}.mp4`;
  const metadata = S3_BASE_URL === DEFAULT_BASE ? matchCatalogue.matches.find((match) => match.match_id === `cam-${i + 1}` && match.source_key === mainKey) : null;
  return {
    id: i + 1,
    label: metadata?.display_name || `Match ${i + 1}`,
    cameraId: `cam-${i + 1}`,
    mainKey,
    sourceKey: mainKey,
    sourceIdentity: metadata?.source_identity?.digest,
    metadata,
    previewKey: PREVIEW_KEY || previewKeyFor(mainKey),
  };
});

const objectUrl = (base, key) => `${base}/${key.split('/').map(encodeURIComponent).join('/')}`;
export const getVideoUrl = (feed) => feed.url || objectUrl(S3_BASE_URL, feed.mainKey || feed.key);
export const getPreviewUrl = (feed) => feed.previewUrl || (feed.url ? feed.url : objectUrl(PREVIEW_BASE_URL, feed.previewKey));
export const sourceOffset = (feed) => Number.isFinite(feed.offsetSeconds) ? feed.offsetSeconds : 0;

// The server registers actual sources. Never derive a signed preview URL by
// modifying its query string; registered sources use the original video unless
// the server explicitly supplies a separate preview URL.
export function registeredFeeds(matches) {
  if (!Array.isArray(matches) || matches.length > 7) return [];
  const seen = new Set();
  return matches.flatMap((match) => {
    if (!match || typeof match.match_id !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(match.match_id) || seen.has(match.match_id)
      || typeof match.label !== 'string' || !match.label || match.label.length > 100 || !Number.isFinite(match.offset_seconds)) return [];
    try { if (!['https:', 'http:'].includes(new URL(match.url).protocol)) return []; } catch { return []; }
    if (match.preview_url) {
      try { if (!['https:', 'http:'].includes(new URL(match.preview_url).protocol)) return []; } catch { return []; }
    }
    seen.add(match.match_id);
    const existing = FEEDS.find((feed) => feed.cameraId === match.match_id);
    const sourceKey = match.source_key || (existing && match.url === getVideoUrl(existing) ? existing.sourceKey : null);
    const metadata = matchCatalogue.matches.find((item) => item.match_id === match.match_id && item.source_key === sourceKey
      && (match.source_identity === item.source_identity?.digest || (match.source_identity == null && existing && match.url === getVideoUrl(existing) && existing.metadata)));
    const serverMetadata = metadata && match.metadata?.match_id === match.match_id && match.metadata.source_key === sourceKey
      && typeof match.source_identity === 'string' && match.metadata.source_identity?.digest === match.source_identity
      ? match.metadata : null;
    return [{
      id: existing?.id ?? match.match_id,
      cameraId: match.match_id,
      label: match.label,
      sourceKey,
      sourceIdentity: typeof match.source_identity === 'string' ? match.source_identity : metadata?.source_identity?.digest,
      metadata: serverMetadata || metadata,
      url: match.url,
      previewUrl: match.preview_url || (existing && match.url === getVideoUrl(existing) ? getPreviewUrl(existing) : undefined),
      offsetSeconds: match.offset_seconds,
      healthy: match.healthy !== false,
    }];
  });
}

// Maps a backend `target_feed` ("cam-3", "3" or 3) to a feed id, or null if unknown.
export function feedIdFromTarget(target) {
  const match = String(target ?? '').trim().match(/(\d+)$/);
  const id = match ? Number(match[1]) : NaN;
  return FEEDS.some((feed) => feed.id === id) ? id : null;
}
