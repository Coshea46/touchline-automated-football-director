import catalogue from '../../../data/matches.json';

function configured(access) {
  const previous = process.env.REACT_APP_SOURCE_ACCESS;
  if (access === undefined) delete process.env.REACT_APP_SOURCE_ACCESS;
  else process.env.REACT_APP_SOURCE_ACCESS = access;
  let result;
  try { jest.isolateModules(() => { result = require('./feeds'); }); }
  finally {
    if (previous === undefined) delete process.env.REACT_APP_SOURCE_ACCESS;
    else process.env.REACT_APP_SOURCE_ACCESS = previous;
  }
  return result;
}

const verified = () => {
  const fixture = catalogue.matches[0];
  const digest = 'a'.repeat(64);
  return { match_id: fixture.match_id, label: fixture.display_name, source_key: fixture.source_key,
    source_identity: digest, fixture_verified: true, offset_seconds: 120,
    url: 'https://private.example/match-1.mp4?signature=first', url_expires_at: Date.now() / 1000 + 3600,
    metadata: { ...fixture, source_identity: { ...fixture.source_identity, digest } } };
};

test('private defaults keep seven labelled placeholders without anonymous video or preview URLs', () => {
  const sources = configured();
  expect(sources.FEEDS).toHaveLength(7);
  expect(new Set(sources.FEEDS.map((feed) => feed.sourceKey)).size).toBe(7);
  sources.FEEDS.forEach((feed) => {
    expect(sources.getVideoUrl(feed)).toBe(''); expect(sources.getPreviewUrl(feed)).toBe('');
    expect(feed.metadata).toBeNull(); expect(feed.sourceIdentity).toBeUndefined();
  });
  expect(sources.FEEDS.map((feed) => feed.label)).toEqual(catalogue.matches.map((fixture) => fixture.display_name));
  expect(configured('unexpected').getVideoUrl(sources.FEEDS[0])).toBe('');
});

test('public fallback and old fixture metadata require an explicit public opt-in', () => {
  const sources = configured('public');
  expect(sources.getVideoUrl(sources.FEEDS[0])).toContain('/match-1.mp4');
  expect(sources.FEEDS[0].metadata).toEqual(catalogue.matches[0]);
  expect(sources.getVideoUrl({ ...sources.FEEDS[0], sourceAccess: 'private' })).toBe('');
});

test('attested migrated metadata binds the actual signed source identity to the canonical fixture', () => {
  const sources = configured();
  const record = verified();
  const [feed] = sources.registeredFeeds([record]);
  expect(feed.metadata).toEqual(record.metadata);
  expect(feed.sourceIdentity).toBe(record.source_identity);
  expect(feed.urlExpiresAt).toBe(record.url_expires_at);
  expect(feed.offsetSeconds).toBe(120);
  expect(sources.getPreviewUrl(feed)).toBe(record.url);
  const [rotated] = sources.registeredFeeds([{ ...record, url: record.url + '-rotated' }]);
  expect(sources.playbackIdentity(rotated)).toBe(sources.playbackIdentity(feed));
  expect(sources.playbackIdentity({ ...feed, sourceIdentity: 'different' })).not.toBe(sources.playbackIdentity(feed));
});

test.each(['attestation', 'digest', 'match', 'key', 'home', 'away', 'display', 'scores'])(
  'rejects migrated metadata with mismatched %s while keeping the registered source distinct', (kind) => {
    const sources = configured();
    const record = verified();
    if (kind === 'attestation') record.fixture_verified = false;
    if (kind === 'digest') record.metadata.source_identity = { digest: 'wrong' };
    if (kind === 'match') record.metadata.match_id = 'cam-2';
    if (kind === 'key') record.metadata.source_key = 'other.mp4';
    if (kind === 'home') record.metadata.home_team = 'Unverified home';
    if (kind === 'away') record.metadata.away_team = 'Unverified away';
    if (kind === 'display') record.metadata.display_name = 'Unverified fixture';
    if (kind === 'scores') record.metadata.replay = { ...record.metadata.replay,
      score_intervals: [{ start_seconds: 0, end_seconds: 10000, home: 9, away: 9, verified: true }] };
    const [feed] = sources.registeredFeeds([record]);
    expect(feed.metadata).toBeUndefined();
    expect(feed.sourceIdentity).toBe(record.source_identity);
    expect(feed.url).toBe(record.url);
  });

test.each([NaN, Infinity, -1, '3600'])('rejects malformed URL expiry %s', (expires) => {
  expect(configured().registeredFeeds([{ ...verified(), url_expires_at: expires }])).toEqual([]);
});

test('registration maps server expiry to a local timer without assuming synchronized browser clocks', () => {
  const sources = configured();
  const serverTime = 1000000000;
  const [feed] = sources.registeredFeeds([{ ...verified(), url_expires_at: serverTime + 120 }], serverTime);
  expect(feed.urlExpiresAt).toBe(serverTime + 120);
  expect(feed.urlExpiresAtLocal).toBeCloseTo(Date.now() / 1000 + 120, 1);
});
