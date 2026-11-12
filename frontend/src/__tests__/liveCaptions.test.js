import { act, fireEvent, render, screen } from '@testing-library/react';
import App from '../app/App';
import CommentaryCaptions from '../features/captions/CommentaryCaptions';
import { FEEDS, getVideoUrl } from '../features/watch/sources/feeds';
import { LIVE_CAPTION_MODEL, readLiveCaption } from '../features/captions/liveCaptions';
import { registeredFeeds } from '../features/watch/sources/feeds';

const baseSession = (overrides = {}) => ({ type: 'session', protocol_version: 1, session_id: 'live-test', sequence: 1,
  state: 'disabled', mode: 'disabled', model_version: 'no-ml', server_time: Date.now() / 1000,
  controls_enabled: true, auto_enabled: true, manual_hold_until: 0, current_match_id: 'cam-1',
  playback: { position: 0, playing: true, rate: 1, clock_epoch: 0 },
  captions: { available: true, enabled: true, user_enabled: true, status: 'connecting', model: LIVE_CAPTION_MODEL, generation: 1, match_id: 'cam-1', clock_epoch: 0 },
  matches: FEEDS.map((feed) => ({ match_id: feed.cameraId, label: feed.label, url: getVideoUrl(feed),
    source_key: feed.sourceKey, source_identity: feed.sourceIdentity, offset_seconds: 120, healthy: true })), ...overrides });
const event = (overrides = {}) => ({ type: 'caption', session_id: 'live-test', sequence: 2, generation: 1, clock_epoch: 0,
  match_id: 'cam-1', source_key: FEEDS[0].sourceKey, source_identity: FEEDS[0].sourceIdentity,
  state: 'partial', text: 'TEST live draft.', provider: 'elevenlabs', model: LIVE_CAPTION_MODEL,
  source_anchor_seconds: 120, received_at: Date.now() / 1000, ...overrides });
function ready(video) {
  Object.defineProperty(video, 'readyState', { configurable: true, value: 2 });
  Object.defineProperty(video, 'duration', { configurable: true, value: 1000 });
  fireEvent.loadedMetadata(video); fireEvent.canPlay(video);
}
function join(overrides = {}) {
  render(<App />); const socket = global.FakeWebSocket.instances.at(-1);
  act(() => { socket.open(); socket.receive(baseSession(overrides)); });
  const video = screen.getByLabelText(`${FEEDS[0].label} video`); ready(video);
  return { socket, video, receive: (value) => act(() => socket.receive(value)) };
}
beforeEach(() => { localStorage.clear(); window.history.replaceState(null, '', '/'); global.fetch = jest.fn(); });
afterEach(() => { delete global.fetch; jest.useRealTimers(); });

test('live captions use the existing backend connection, replace drafts with committed text and never fetch a batch track', () => {
  localStorage.setItem('matchpulse-captions-enabled', 'true');
  const { socket, video, receive } = join();
  expect(screen.getByRole('button', { name: /Commentary captions On/ })).toBeInTheDocument();
  expect(socket.sent.filter((item) => item.type === 'captions')).toHaveLength(0);
  receive(event()); expect(screen.getByText('TEST live draft.')).toBeInTheDocument();
  expect(screen.getByText('TEST live draft.').closest('[aria-live]')).toHaveAttribute('aria-live', 'polite');
  receive(event({ sequence: 3, state: 'committed', text: 'TEST corrected final.', words: [{ text: 'TEST', start_seconds: 120, end_seconds: 120 }] }));
  expect(screen.queryByText('TEST live draft.')).not.toBeInTheDocument();
  expect(screen.getByText('TEST corrected final.')).toBeInTheDocument();
  expect(screen.getByText('TEST corrected final.').closest('[aria-live]')).toHaveAttribute('aria-atomic', 'true');
  expect(video.currentTime).toBe(120); expect(fetch).not.toHaveBeenCalled();
  expect(socket.url).not.toContain('elevenlabs'); expect(socket.url).not.toContain('token');
  expect(screen.getByRole('link', { name: 'ElevenLabs' }).closest('p')).toHaveTextContent(/draft text may change/i);
  expect(screen.getByRole('link', { name: 'ElevenLabs' }).closest('p')).toHaveTextContent(/not human reviewed/i);
});

test('visibility toggle sends only controller captions commands, persists preference and clears current text', () => {
  const { socket, video, receive } = join({ captions: { ...baseSession().captions, user_enabled: false, enabled: false, status: 'disabled' } });
  video.currentTime = 155; fireEvent.playing(video);
  fireEvent.click(screen.getByRole('button', { name: /Commentary captions Off/ }));
  expect(socket.sent).toContainEqual({ type: 'captions', session_id: 'live-test', enabled: true });
  receive(baseSession({ sequence: 2, captions: { ...baseSession().captions, generation: 2 } }));
  receive(event({ sequence: 3, generation: 2, source_anchor_seconds: 155 })); expect(screen.getByText('TEST live draft.')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /Commentary captions On/ }));
  expect(socket.sent.at(-1)).toEqual({ type: 'captions', session_id: 'live-test', enabled: false });
  expect(screen.queryByText('TEST live draft.')).not.toBeInTheDocument();
  expect(localStorage.getItem('matchpulse-captions-enabled')).toBe('false');
  expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBe(video); expect(video.currentTime).toBe(155);
  expect(socket.sent.filter((item) => item.type === 'playback').at(-1).position).toBe(35);
});

test('an observer can toggle local visibility without starting or stopping a provider stream', () => {
  localStorage.setItem('matchpulse-captions-enabled', 'true'); const { socket } = join({ controls_enabled: false });
  fireEvent.click(screen.getByRole('button', { name: /Commentary captions On/ }));
  fireEvent.click(screen.getByRole('button', { name: /Commentary captions Off/ }));
  expect(socket.sent.filter((item) => item.type === 'captions')).toHaveLength(0); expect(fetch).not.toHaveBeenCalled();
});

test('an observer becoming controller adopts server intent without changing the provider request', () => {
  localStorage.setItem('matchpulse-captions-enabled', 'true'); const { socket, receive } = join({ controls_enabled: false });
  expect(screen.getByRole('button', { name: /Commentary captions On/ })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /Commentary captions On/ }));
  receive(baseSession({ sequence: 2, controls_enabled: true }));
  expect(screen.getByRole('button', { name: /Commentary captions On/ })).toBeInTheDocument();
  expect(socket.sent.filter((item) => item.type === 'captions' && item.enabled)).toHaveLength(0);
});

test('caption validation rejects stale order, sessions, generation, epoch, source, model and future source words', () => {
  const session = baseSession(); session.feeds = registeredFeeds(session.matches);
  const playback = { playing: true, available: true, stalled: false, sourceTime: 120 };
  expect(readLiveCaption(event(), session, session.feeds[0], playback)).not.toBeNull();
  expect(readLiveCaption(event({ state: 'listening', text: '', source_anchor_seconds: undefined }), session, session.feeds[0], playback)).not.toBeNull();
  for (const invalid of [{ sequence: 1 }, { session_id: 'other' }, { generation: 0 }, { generation: 2 }, { clock_epoch: 1 },
    { match_id: 'cam-2' }, { source_key: 'wrong.mp4' }, { source_identity: 'wrong' }, { model: 'scribe_v2' },
    { provider: 'unknown' }, { state: 'invented' }, { source_anchor_seconds: 121 },
    { state: 'committed', words: [{ text: 'future', start_seconds: 120, end_seconds: 122 }] }]) {
    expect(readLiveCaption(event(invalid), session, session.feeds[0], playback)).toBeNull();
  }
  expect(readLiveCaption(event(), session, session.feeds[0], { ...playback, sourceTime: undefined })).toBeNull();
  expect(readLiveCaption(event(), session, session.feeds[0], playback, 2)).toBeNull();
});

test('pause immediately clears live text and an old generation cannot reappear after resume', () => {
  localStorage.setItem('matchpulse-captions-enabled', 'true'); const { video, receive } = join();
  receive(event()); fireEvent.pause(video); expect(screen.queryByText('TEST live draft.')).not.toBeInTheDocument();
  receive(event({ sequence: 3, text: 'TEST stale paused.' })); expect(screen.queryByText('TEST stale paused.')).not.toBeInTheDocument();
  fireEvent.playing(video); receive(event({ sequence: 4, text: 'TEST stale resumed.' }));
  expect(screen.queryByText('TEST stale resumed.')).not.toBeInTheDocument();
  receive(baseSession({ sequence: 5, captions: { ...baseSession().captions, generation: 2 } }));
  receive(event({ sequence: 6, generation: 2, text: 'TEST new resumed.' })); expect(screen.getByText('TEST new resumed.')).toBeInTheDocument();
  expect(video.currentTime).toBe(120);
});

test('seek clears caption context and requires matching new clock epoch and generation', () => {
  localStorage.setItem('matchpulse-captions-enabled', 'true'); const { video, receive } = join();
  receive(event()); video.currentTime = 200; fireEvent.seeking(video);
  expect(screen.queryByText('TEST live draft.')).not.toBeInTheDocument();
  receive(baseSession({ sequence: 3, playback: { position: 80, playing: true, rate: 1, clock_epoch: 1 },
    captions: { ...baseSession().captions, generation: 2, clock_epoch: 1 } }));
  fireEvent.seeked(video);
  receive(event({ sequence: 4, text: 'TEST old seek.' })); expect(screen.queryByText('TEST old seek.')).not.toBeInTheDocument();
  receive(event({ sequence: 5, generation: 2, clock_epoch: 1, source_anchor_seconds: 200, text: 'TEST new seek.' }));
  expect(screen.getByText('TEST new seek.')).toBeInTheDocument(); expect(video.currentTime).toBe(200);
});

test('a media error clears live text and rejects late text from the broken stream', () => {
  localStorage.setItem('matchpulse-captions-enabled', 'true'); const { video, receive } = join();
  receive(event()); fireEvent.error(video); expect(screen.queryByText('TEST live draft.')).not.toBeInTheDocument();
  receive(event({ sequence: 3, text: 'TEST broken stream.' })); expect(screen.queryByText('TEST broken stream.')).not.toBeInTheDocument();
  expect(screen.getByText('Live commentary captions are unavailable right now.')).toBeInTheDocument();
});

test('disconnect clears text and reconnect resumes authoritative On without an extra enable command', () => {
  jest.useFakeTimers(); localStorage.setItem('matchpulse-captions-enabled', 'true'); const { socket, receive } = join();
  receive(event()); act(() => socket.drop()); expect(screen.queryByText('TEST live draft.')).not.toBeInTheDocument();
  act(() => jest.advanceTimersByTime(1000)); const next = global.FakeWebSocket.instances.at(-1);
  act(() => { next.open(); next.receive(baseSession({ sequence: 3, captions: { ...baseSession().captions, generation: 2 } }));
    next.receive(baseSession({ sequence: 4, captions: { ...baseSession().captions, generation: 2 } })); });
  expect(next.sent.filter((item) => item.type === 'captions')).toHaveLength(0);
  expect(screen.getByRole('button', { name: /Commentary captions On/ })).toBeInTheDocument();
});

test('authoritative server On intent overrides an old local Off preference without an enable command', () => {
  localStorage.setItem('matchpulse-captions-enabled', 'false'); const { socket, receive } = join();
  expect(screen.getByRole('button', { name: /Commentary captions On/ })).toBeInTheDocument();
  expect(socket.sent.filter((item) => item.type === 'captions' && item.enabled)).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: /Commentary captions On/ }));
  receive(baseSession({ sequence: 2, captions: { ...baseSession().captions, user_enabled: false, enabled: false, generation: 2, status: 'disabled' } }));
  receive(baseSession({ session_id: 'new-live-session' }));
  expect(screen.getByRole('button', { name: /Commentary captions On/ })).toBeInTheDocument();
  expect(socket.sent.filter((item) => item.type === 'captions' && item.session_id === 'new-live-session')).toHaveLength(0);
});

test('captions start Off before a session and an old local On preference cannot enable a fresh server-Off session', () => {
  localStorage.setItem('matchpulse-captions-enabled', 'true');
  render(<App />);
  expect(screen.getByRole('button', { name: /Commentary captions Off/ })).toBeInTheDocument();
  const socket = global.FakeWebSocket.instances.at(-1);
  act(() => { socket.open(); socket.receive(baseSession({ captions: { ...baseSession().captions,
    user_enabled: false, enabled: false, status: 'off' } })); });
  expect(screen.getByRole('button', { name: /Commentary captions Off/ })).toBeInTheDocument();
  expect(socket.sent.filter((item) => item.type === 'captions')).toEqual([]);
});

test('Off stays Off through pending state, source changes and further snapshots without automatic enable commands', () => {
  const { socket, video, receive } = join(); receive(event());
  fireEvent.click(screen.getByRole('button', { name: /Commentary captions On/ }));
  receive(baseSession({ sequence: 3 })); // Snapshot already in transit before the Off acknowledgement.
  expect(screen.getByRole('button', { name: /Commentary captions Off/ })).toBeInTheDocument();
  const off = { ...baseSession().captions, user_enabled: false, enabled: false, generation: 2, status: 'disabled', reason: 'viewer_disabled' };
  receive(baseSession({ sequence: 4, captions: off }));
  fireEvent.click(screen.getByRole('button', { name: FEEDS[1].label })); ready(video);
  receive(baseSession({ sequence: 5, current_match_id: 'cam-2', captions: { ...off, generation: 3, match_id: 'cam-2' } }));
  receive(baseSession({ sequence: 6, current_match_id: 'cam-2', captions: { ...off, generation: 3, match_id: 'cam-2' } }));
  expect(screen.getByRole('button', { name: /Commentary captions Off/ })).toBeInTheDocument();
  expect(screen.queryByText('TEST live draft.')).not.toBeInTheDocument();
  expect(socket.sent.filter((item) => item.type === 'captions')).toEqual([{ type: 'captions', session_id: 'live-test', enabled: false }]);
});

test('same-session reconnect adopts server Off without resetting to the new-session On default', () => {
  jest.useFakeTimers(); const { socket, receive } = join();
  fireEvent.click(screen.getByRole('button', { name: /Commentary captions On/ }));
  const off = { ...baseSession().captions, user_enabled: false, enabled: false, generation: 2, status: 'disabled' };
  receive(baseSession({ sequence: 2, captions: off })); act(() => socket.drop());
  act(() => jest.advanceTimersByTime(1000)); const next = global.FakeWebSocket.instances.at(-1);
  act(() => { next.open(); next.receive(baseSession({ sequence: 3, captions: { ...off, generation: 3 } })); });
  expect(screen.getByRole('button', { name: /Commentary captions Off/ })).toBeInTheDocument();
  expect(next.sent.filter((item) => item.type === 'captions')).toHaveLength(0);
});

test('an explicit offline Off is replayed once on the same session instead of losing viewer intent', () => {
  jest.useFakeTimers(); const { socket } = join(); act(() => socket.drop());
  fireEvent.click(screen.getByRole('button', { name: /Commentary captions On/ }));
  act(() => jest.advanceTimersByTime(1000)); const next = global.FakeWebSocket.instances.at(-1);
  act(() => { next.open(); next.receive(baseSession({ sequence: 2 })); next.receive(baseSession({ sequence: 3 })); });
  expect(screen.getByRole('button', { name: /Commentary captions Off/ })).toBeInTheDocument();
  expect(next.sent.filter((item) => item.type === 'captions')).toEqual([{ type: 'captions', session_id: 'live-test', enabled: false }]);
});

test('Off chosen before the first session reaches the server before actual playback can start captions', () => {
  render(<App />); const socket = global.FakeWebSocket.instances.at(-1);
  fireEvent.click(screen.getByRole('button', { name: /Commentary captions Off/ }));
  fireEvent.click(screen.getByRole('button', { name: /Commentary captions On/ }));
  act(() => { socket.open(); socket.receive(baseSession()); });
  expect(screen.getByRole('button', { name: /Commentary captions Off/ })).toBeInTheDocument();
  expect(socket.sent.find((item) => item.type === 'captions')).toEqual({ type: 'captions', session_id: 'live-test', enabled: false });
  expect(socket.sent.findIndex((item) => item.type === 'captions')).toBeLessThan(socket.sent.findIndex((item) => item.type === 'playback'));
});

test('an exhausted original budget remains visible after an explicit On request without retry commands', () => {
  const off = { ...baseSession().captions, user_enabled: false, enabled: false, status: 'disabled', reason: 'viewer_disabled' };
  const { socket, receive } = join({ captions: off });
  fireEvent.click(screen.getByRole('button', { name: /Commentary captions Off/ }));
  receive(baseSession({ sequence: 2, captions: { ...off, user_enabled: true, status: 'stopped', reason: 'budget_deadline' } }));
  expect(screen.getByRole('button', { name: /Commentary captions On/ })).toBeInTheDocument();
  expect(screen.getByText('Live caption session allowance has been reached.')).toBeInTheDocument();
  receive(baseSession({ sequence: 3, captions: { ...off, user_enabled: true, status: 'stopped', reason: 'budget_deadline' } }));
  expect(socket.sent.filter((item) => item.type === 'captions')).toEqual([{ type: 'captions', session_id: 'live-test', enabled: true }]);
});

test.each([['missing_key', /operator needs to connect ElevenLabs/], ['auth_error', /operator to check Speech to Text access/]])('default-On panel exposes %s without retrying the failed provider automatically', (reason, label) => {
  const { socket, receive } = join({ captions: { ...baseSession().captions, user_enabled: true, enabled: false, available: false, status: 'unavailable', reason } });
  expect(screen.getByRole('button', { name: /Commentary captions On/ })).toBeInTheDocument();
  expect(screen.getByText(label)).toBeInTheDocument();
  receive(baseSession({ sequence: 2, captions: { ...baseSession().captions, user_enabled: true, enabled: false, available: false, status: 'unavailable', reason } }));
  expect(socket.sent.filter((item) => item.type === 'captions')).toHaveLength(0);
});

test.each([['connecting', null, /Connecting live commentary/], ['listening', null, /Listening to the focused match/], ['paused', null, /pause with the player/],
  ['stopped', 'budget_deadline', /session allowance has been reached/], ['error', 'rate_limited', /temporarily limited/],
  ['unavailable', 'unknown_provider_status', /unavailable right now/], ['disabled', 'feature_disabled', /disabled for this demo/],
  ['error', 'auth_error', /operator to check Speech to Text access/],
  ['unavailable', 'provider_permission_speech_to_text_missing', /operator to check Speech to Text access/],
  ['unavailable', 'caption_runtime_missing', /needs attention from the site operator/],
  ['unavailable', 'caption_worker_unavailable', /needs attention from the site operator/],
  ['unavailable', 'caption_worker_ended', /needs attention from the site operator/],
  ['unavailable', 'missing_key', /operator needs to connect ElevenLabs/]])('live caption status %s stays honest without requesting a batch track', (status, reason, label) => {
  const ref = { current: null };
  render(<CommentaryCaptions feed={FEEDS[0]} mediaRef={ref} enabled live={{ status, reason, available: true, text: '' }} />);
  expect(screen.getByText(label)).toBeInTheDocument(); expect(fetch).not.toHaveBeenCalled();
});
