import matchCatalogue from '../../../data/matches.json';

const DEFAULT_BASE = 'https://match-switcher-poc-videos.s3.eu-west-2.amazonaws.com';
const S3_BASE_URL = (process.env.REACT_APP_S3_BASE_URL || DEFAULT_BASE).replace(/\/$/, '');
const PREVIEW_BASE_URL = (process.env.REACT_APP_PREVIEW_BASE_URL || S3_BASE_URL).replace(/\/$/, '');
const TEST_VIDEO_KEY = process.env.REACT_APP_TEST_VIDEO_KEY;
const PREVIEW_KEY = process.env.REACT_APP_TEST_PREVIEW_KEY;
const PUBLIC_DEMO = process.env.REACT_APP_SOURCE_ACCESS === 'public';

// Each match is match-<n>.mp4 at the bucket root. Its lightweight preview
// (used by the small tiles) sits next to it as match-<n>-preview-v1.mp4.
// REACT_APP_TEST_VIDEO_KEY / REACT_APP_TEST_PREVIEW_KEY force every feed to one video, for testing.
// Public fallback is an explicit demo/test opt-in. Private playback waits for registration.
const previewKeyFor = (key) => key.replace(/\.mp4$/i, '-preview-v1.mp4');

export const FEEDS = Array.from({ length: 7 }, (_, i) => {
  const mainKey = TEST_VIDEO_KEY || `match-${i + 1}.mp4`;
  const fixture = matchCatalogue.matches.find((match) => match.match_id === `cam-${i + 1}` && match.source_key === mainKey);
  const metadata = PUBLIC_DEMO && S3_BASE_URL === DEFAULT_BASE ? fixture : null;
  return {
    id: i + 1,
    label: fixture?.display_name || `Match ${i + 1}`,
    cameraId: `cam-${i + 1}`,
    mainKey,
    sourceKey: mainKey,
    sourceIdentity: metadata?.source_identity?.digest,
    metadata,
    previewKey: PREVIEW_KEY || previewKeyFor(mainKey),
    sourceAccess: PUBLIC_DEMO ? 'public' : 'private',
  };
});

const objectUrl = (base, key) => `${base}/${key.split('/').map(encodeURIComponent).join('/')}`;
export const getVideoUrl = (feed) => feed.url || (PUBLIC_DEMO && feed.sourceAccess !== 'private' ? objectUrl(S3_BASE_URL, feed.mainKey || feed.key) : '');
export const getPreviewUrl = (feed) => feed.previewUrl || (feed.url ? feed.url : PUBLIC_DEMO && feed.sourceAccess !== 'private' ? objectUrl(PREVIEW_BASE_URL, feed.previewKey) : '');
export const sourceOffset = (feed) => Number.isFinite(feed.offsetSeconds) ? feed.offsetSeconds : 0;

// Authorization URLs may rotate while the recording, fixture and clock stay the same.
export const playbackIdentity = (feed) => feed.sourceKey && feed.sourceIdentity
  ? `${feed.cameraId}\n${feed.sourceKey}\n${feed.sourceIdentity}` : `${getVideoUrl(feed)}\n${feed.cameraId}`;

const sameFixture = (metadata, fixture) => ['home_team', 'away_team', 'display_name'].every((field) => metadata[field] === fixture[field])
  && Array.isArray(metadata.replay?.score_intervals)
  && metadata.replay.score_intervals.length === fixture.replay.score_intervals.length
  && metadata.replay.score_intervals.every((interval, index) => interval && ['start_seconds', 'end_seconds', 'home', 'away', 'verified']
    .every((field) => interval[field] === fixture.replay.score_intervals[index][field]));

// The server registers actual sources. Never derive a signed preview URL by
// modifying its query string; registered sources use the original video unless
// the server explicitly supplies a separate preview URL.
export function registeredFeeds(matches, serverTime = null) {
  if (!Array.isArray(matches) || matches.length > 7) return [];
  const seen = new Set();
  return matches.flatMap((match) => {
    if (!match || typeof match.match_id !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(match.match_id) || seen.has(match.match_id)
      || typeof match.label !== 'string' || !match.label || match.label.length > 100 || !Number.isFinite(match.offset_seconds)) return [];
    if (match.url_expires_at != null && (!Number.isFinite(match.url_expires_at) || match.url_expires_at <= 0)) return [];
    try { if (!['https:', 'http:'].includes(new URL(match.url).protocol)) return []; } catch { return []; }
    if (match.preview_url) {
      try { if (!['https:', 'http:'].includes(new URL(match.preview_url).protocol)) return []; } catch { return []; }
    }
    seen.add(match.match_id);
    const existing = FEEDS.find((feed) => feed.cameraId === match.match_id);
    const sourceKey = match.source_key || (existing && match.url === getVideoUrl(existing) ? existing.sourceKey : null);
    const metadata = PUBLIC_DEMO ? matchCatalogue.matches.find((item) => item.match_id === match.match_id && item.source_key === sourceKey
      && (match.source_identity === item.source_identity?.digest || (match.source_identity == null && existing && match.url === getVideoUrl(existing) && existing.metadata))) : undefined;
    const fixture = matchCatalogue.matches.find((item) => item.match_id === match.match_id && item.source_key === sourceKey);
    const serverMetadata = fixture && match.fixture_verified === true && match.metadata?.match_id === match.match_id && match.metadata.source_key === sourceKey
      && typeof match.source_identity === 'string' && match.metadata.source_identity?.digest === match.source_identity
      && sameFixture(match.metadata, fixture)
      ? match.metadata : null;
    return [{
      id: existing?.id ?? match.match_id,
      cameraId: match.match_id,
      label: match.label,
      sourceKey,
      sourceIdentity: typeof match.source_identity === 'string' ? match.source_identity : metadata?.source_identity?.digest,
      metadata: serverMetadata || metadata,
      url: match.url,
      urlExpiresAt: match.url_expires_at ?? null,
      urlExpiresAtLocal: Number.isFinite(match.url_expires_at) && Number.isFinite(serverTime)
        ? Date.now() / 1000 + Math.max(0, match.url_expires_at - serverTime) : null,
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
