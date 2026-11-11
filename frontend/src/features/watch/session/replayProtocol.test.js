import { readSession, validRecommendation } from './replayProtocol';
import { registeredFeeds } from '../sources/feeds';

const envelope = (extra = {}) => ({
  type: 'session', protocol_version: 1, session_id: 'one', model_version: 'gru-v1', mode: 'real', sequence: 1,
  server_time: 1000, playback: { position: 12, clock_epoch: 3 },
  matches: [{ match_id: 'cam-1', label: 'Match 1', url: 'https://example.com/video.mp4', offset_seconds: 8, healthy: true }],
  ...extra,
});
const rec = (extra = {}) => ({
  type: 'recommendation', protocol_version: 1, session_id: 'one', model_version: 'gru-v1', sequence: 2,
  clock_epoch: 3, score_kind: 'model', selected_match_id: 'cam-1', scores: { 'cam-1': 0.6 },
  playback_time: 12, observation_end: 11, emitted_at: 999.8, valid_until: 1002, ...extra,
});

test('registers explicit source identity/offset without rewriting signed URLs', () => {
  const source = envelope().matches[0];
  const [feed] = registeredFeeds([{ ...source, url: `${source.url}?token=opaque` }]);
  expect(feed).toMatchObject({ id: 1, cameraId: 'cam-1', offsetSeconds: 8, url: `${source.url}?token=opaque` });
});

test('rejects duplicate identities, unsafe URLs and malformed session/timeline envelopes', () => {
  const source = envelope().matches[0];
  [
    { protocol_version: 2 }, { session_id: '' }, { model_version: null }, { mode: 'unknown' },
    { sequence: 1.5 }, { server_time: NaN }, { playback: { position: -1 } },
    { auto_available: 'true' }, { auto_available: 1 }, { auto_available: null },
    { playback: { position: 0, clock_epoch: -1 } },
    { matches: [source, source] }, { matches: [{ ...source, url: 'javascript:alert(1)' }] },
    { matches: [{ ...source, offset_seconds: '8' }] }, { matches: [] },
  ].forEach((extra) => expect(readSession(envelope(extra))).toBeNull());
  expect(readSession(envelope(), readSession(envelope()))).toBeNull();
  expect(readSession(envelope({ sequence: 2 }), readSession(envelope()))).not.toBeNull();
});

test('preserves explicit Auto readiness separately from requested and effective state', () => {
  const waiting = readSession(envelope({ state: 'starting', auto_available: false,
    auto_requested: true, auto_enabled: false }));
  expect(waiting).toMatchObject({ auto_available: false, auto_requested: true, auto_enabled: false });
  const running = readSession(envelope({ state: 'running', auto_available: true,
    auto_requested: true, auto_enabled: true, sequence: 2 }), waiting);
  expect(running).toMatchObject({ auto_available: true, auto_enabled: true });
  expect(readSession(envelope())).not.toBeNull(); // Older active session envelopes remain compatible.
});

test('optional Auto request, start capability, ownership capability and startup fields accept only booleans', () => {
  for (const field of ['auto_requested', 'auto_can_start', 'auto_starting', 'can_claim_control']) {
    for (const value of [true, false]) {
      expect(readSession(envelope({ [field]: value }))).toMatchObject({ [field]: value });
    }
    for (const value of [null, 0, 1, 'true', 'false', {}, []]) {
      expect(readSession(envelope({ [field]: value }))).toBeNull();
    }
  }
  expect(readSession(envelope())).not.toBeNull();
  expect(readSession(envelope({ state: 'starting', auto_requested: true, auto_can_start: false,
    auto_starting: true, auto_available: false, auto_enabled: false }))).toMatchObject({
    auto_requested: true, auto_can_start: false, auto_starting: true, auto_available: false, auto_enabled: false,
  });
});

test('requested startup cannot authorize a real recommendation before explicit model readiness', () => {
  const pending = readSession(envelope({ state: 'starting', auto_requested: true,
    auto_starting: true, auto_can_start: false, auto_available: false, auto_enabled: false }));
  expect(validRecommendation(rec(), pending, { time: 12 }, 1000)).toBe(false);
  expect(validRecommendation(rec({ selection_kind: 'fallback', selection_reason: 'unavailable_fallback' }),
    pending, { time: 12, available: false, stalled: true }, 1000)).toBe(false);
  const ready = readSession(envelope({ state: 'running', auto_requested: true,
    auto_starting: false, auto_available: true, auto_enabled: true }));
  expect(validRecommendation(rec(), ready, { time: 12 }, 1000)).toBe(true);
  expect(validRecommendation(rec(), readSession(envelope()), { time: 12 }, 1000)).toBe(true);
});

test('accepts the completed observation behind the actual viewer clock only', () => {
  const session = readSession(envelope());
  expect(validRecommendation(rec(), session, { time: 12 }, 1000)).toBe(true);
  [
    { observation_end: 12.051 }, { playback_time: 12.251 }, { observation_end: 8.9 },
    { playback_time: 8.9 }, { emitted_at: 996.9 }, { valid_until: 999.9 },
    { emitted_at: 1001.1 }, { valid_until: 999.7 }, { sequence: 1 },
    { sequence: 2.5 }, { score_kind: 'mock' }, { clock_epoch: 2 },
    { session_id: 'previous' }, { model_version: 'different' },
    { scores: { 'cam-1': 2 } }, { scores: { 'cam-1': NaN } }, { scores: {} },
    { scores: { 'cam-1': 0.6, unknown: 0.5 } },
  ].forEach((extra) => expect(validRecommendation(rec(extra), session, { time: 12 }, 1000)).toBe(false));
  expect(validRecommendation(rec(), session, { time: 12, stalled: true }, 1000)).toBe(false);
  expect(validRecommendation(rec(), session, { time: 12, available: false }, 1000)).toBe(false);
  expect(validRecommendation(rec({ selection_kind: 'fallback', selection_reason: 'unavailable_fallback' }),
    session, { time: 12, available: false, stalled: true }, 1000)).toBe(true);
  expect(validRecommendation(rec({ selection_kind: 'fallback', selection_reason: 'unavailable_fallback' }),
    session, { time: 12, available: true, stalled: true }, 1000)).toBe(false);
  expect(validRecommendation(rec({ score_kind: 'mock' }), readSession(envelope({ mode: 'disabled' })), { time: 12 }, 1000)).toBe(false);
});
