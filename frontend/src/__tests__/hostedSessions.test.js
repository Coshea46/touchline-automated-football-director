import { act, fireEvent, render, screen } from '@testing-library/react';
import App from '../app/App';
import { FEEDS, getVideoUrl } from '../features/watch/sources/feeds';
import { VISITOR_STORAGE_KEY } from '../features/watch/session/useVisitorSession';

jest.mock('../features/watch/session/useVisitorSession', () => {
  const actual = jest.requireActual('../features/watch/session/useVisitorSession');
  return { __esModule: true, ...actual, INDEPENDENT_SESSIONS: true, default: () => actual.default(true) };
});

const id = 'hosted_session_1234567890abcdef';
const token = 'hosted_resume_1234567890abcdefghijklmnop';
const auth = (resumed = false, sessionId = id) => ({ type: 'session_auth', session_id: sessionId,
  resume_token: token, resumed, expires_at: Date.now() / 1000 + 1200 });
const snapshot = (sequence = 1, position = 0, extra = {}) => ({
  type: 'session', protocol_version: 1, session_id: id, model_version: 'test-model', mode: 'real', state: 'running',
  sequence, server_time: Date.now() / 1000, current_match_id: 'cam-1', controls_enabled: true, can_claim_control: false,
  auto_available: true, auto_enabled: true, auto_requested: true, auto_can_start: false, auto_starting: false,
  auto_start_reason: null, playback: { position, clock_epoch: 0 },
  matches: FEEDS.map((feed) => ({ match_id: feed.cameraId, label: feed.label, url: getVideoUrl(feed),
    source_key: feed.sourceKey, source_identity: feed.sourceIdentity, offset_seconds: 0, healthy: true })),
  director: { mode: 'balanced', config: { cooldown_seconds: 12, staying_bonus: .2, confirmations_required: 2 }, latest_scores: {} },
  captions: { enabled: false, user_enabled: false, available: true, status: 'unavailable', reason: 'awaiting_start',
    match_id: 'cam-1', generation: 0, clock_epoch: 0 },
  session_expires_at: Date.now() / 1000 + 1200, ai_limit_seconds: 1200, ai_remaining_seconds: 1200,
  ai_active: true, ai_reason: null, ...extra,
});
const socket = () => global.FakeWebSocket.instances.at(-1);
const ready = (video) => {
  Object.defineProperties(video, { duration: { configurable: true, value: 5400 },
    readyState: { configurable: true, value: 2 }, seeking: { configurable: true, value: false } });
  fireEvent.loadedMetadata(video); fireEvent.loadedData(video);
};
const join = (resumed = false, position = 0, extra = {}) => {
  const view = render(<App />);
  act(() => { socket().open(); socket().receive(auth(resumed)); socket().receive(snapshot(1, position, extra)); });
  const videos = [...view.container.querySelectorAll('video')];
  videos.forEach(ready);
  return { ...view, videos, main: () => view.container.querySelector('.tile--main video') };
};

beforeEach(() => {
  sessionStorage.clear(); localStorage.clear(); window.history.replaceState(null, '', '/'); jest.useFakeTimers();
  jest.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  jest.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(function start() {
    Object.defineProperty(this, 'paused', { configurable: true, value: false }); return Promise.resolve();
  });
  jest.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(function stop() {
    Object.defineProperty(this, 'paused', { configurable: true, value: true });
  });
});
afterEach(() => { jest.restoreAllMocks(); jest.useRealTimers(); });

test('a new visitor adopts backend-owned Director startup once with captions Off and all players at zero', () => {
  const view = join(false, 0, { state: 'starting', auto_available: false, auto_enabled: false, auto_starting: true });
  expect(socket().sent[0]).toEqual({ type: 'hello' });
  expect(view.videos).toHaveLength(7);
  view.videos.forEach((video) => expect(video.currentTime).toBe(0));
  expect(screen.getByRole('button', { name: 'Auto starting' })).toHaveAttribute('aria-busy', 'true');
  expect(screen.getByRole('button', { name: 'Commentary captions Off' })).toBeInTheDocument();
  expect(screen.getByText(/Director runs last up to 20 minutes/)).toBeInTheDocument();
  expect(socket().sent.some((message) => message.type === 'auto')).toBe(false);
  expect(socket().sent.some((message) => message.type === 'captions')).toBe(false);
  act(() => socket().receive(snapshot(2)));
  expect(screen.getByRole('button', { name: 'Auto' })).toHaveAttribute('aria-pressed', 'true');
  expect(socket().sent.filter((message) => message.type === 'auto')).toEqual([]);
});

test('ordinary reconnect resumes the capability and current native clock without remounting or rewinding', () => {
  const view = join();
  view.main().currentTime = 25; fireEvent.timeUpdate(view.main());
  const seeks = jest.spyOn(HTMLMediaElement.prototype, 'currentTime', 'set');
  const loads = jest.spyOn(HTMLMediaElement.prototype, 'load');
  act(() => socket().drop());
  view.main().currentTime = 30; fireEvent.timeUpdate(view.main());
  seeks.mockClear(); loads.mockClear();
  act(() => jest.advanceTimersByTime(1000));
  seeks.mockClear(); loads.mockClear(); // Background warming during the gap is ordinary synchronization.
  act(() => { socket().open(); socket().receive(auth(true)); socket().receive(snapshot(2, 25)); });
  expect(socket().sent[0]).toEqual({ type: 'hello', session_id: id, resume_token: token });
  expect(view.main().currentTime).toBe(30);
  view.videos.forEach((video, index) => expect(view.container.querySelectorAll('video')[index]).toBe(video));
  expect(seeks).not.toHaveBeenCalled(); expect(loads).not.toHaveBeenCalled();
  expect(socket().sent.filter((message) => message.type === 'auto')).toEqual([]);
});

test('a page reload resumes its nonzero server position unavailable until the actual seek decodes', () => {
  sessionStorage.setItem(VISITOR_STORAGE_KEY, JSON.stringify(auth()));
  const view = render(<App />);
  act(() => { socket().open(); socket().receive(auth(true)); socket().receive(snapshot(1, 90)); });
  const pending = socket().sent.filter((message) => message.type === 'playback');
  expect(pending.length).toBeGreaterThan(0);
  expect(pending.every((message) => message.position === 90 && message.stalled && !message.available)).toBe(true);
  const videos = [...view.container.querySelectorAll('video')];
  videos.forEach(ready);
  expect(view.container.querySelector('.tile--main video').currentTime).toBe(90);
  expect(socket().sent.filter((message) => message.type === 'playback').at(-1)).toMatchObject({ position: 90, available: true, stalled: false });
});

test('expired sessions remain stopped until explicit fresh replay, resetting existing players only once', () => {
  const view = join();
  view.main().currentTime = 35; fireEvent.timeUpdate(view.main());
  const saved = sessionStorage.getItem(VISITOR_STORAGE_KEY);
  act(() => socket().receive({ type: 'error', code: 'session_expired' }));
  expect(screen.getByRole('button', { name: 'Start new replay' })).toBeInTheDocument();
  expect(view.videos.every((video) => video.paused)).toBe(true);
  expect(sessionStorage.getItem(VISITOR_STORAGE_KEY)).toBe(saved);
  act(() => jest.advanceTimersByTime(60000));
  expect(global.FakeWebSocket.instances).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: 'Start new replay' }));
  expect(sessionStorage.getItem(VISITOR_STORAGE_KEY)).toBeNull();
  act(() => socket().open());
  expect(socket().sent).toEqual([{ type: 'hello' }]);
  // Complete any old background alignment before the new explicit zero target.
  view.videos.forEach((video) => fireEvent.seeked(video));
  view.videos.forEach((video, index) => {
    expect(view.container.querySelectorAll('video')[index]).toBe(video);
    expect(video.currentTime).toBe(0);
  });
  act(() => { socket().receive(auth(false, 'new_hosted_session_1234567890abcdef'));
    socket().receive(snapshot(1, 0, { session_id: 'new_hosted_session_1234567890abcdef' })); });
  view.videos.forEach(ready);
  expect(socket().sent.some((message) => message.type === 'auto')).toBe(false);
  view.main().currentTime = 5;
  act(() => jest.advanceTimersByTime(250));
  expect(socket().sent.filter((message) => message.type === 'playback').at(-1)).toMatchObject({ position: 5 });
});

const directorOff = { mode: 'disabled', state: 'disabled', auto_available: false, auto_requested: false,
  auto_enabled: false, auto_can_start: true, ai_active: false, ai_reason: 'awaiting_start' };
const pendingStartup = { ...directorOff, auto_requested: true, auto_starting: false };
const captionsOn = { ...snapshot().captions, enabled: true, user_enabled: true, status: 'listening', reason: null, generation: 1 };

test('default startup intent can be canceled before the first decoded playback without an enable command', () => {
  const view = render(<App />);
  act(() => { socket().open(); socket().receive(auth()); socket().receive(snapshot(1, 0, pendingStartup)); });
  expect(screen.getByRole('button', { name: 'Auto starting' })).toBeEnabled();
  expect(socket().sent.some((message) => message.type === 'playback' && message.available && !message.stalled)).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: 'Auto starting' }));
  expect(socket().sent.filter((message) => message.type === 'auto')).toEqual([{ type: 'auto', session_id: id, enabled: false }]);
  act(() => socket().receive(snapshot(2, 0, directorOff)));
  [...view.container.querySelectorAll('video')].forEach(ready);
  expect(screen.getByRole('button', { name: 'Auto off' })).toBeInTheDocument();
  expect(socket().sent.filter((message) => message.type === 'auto' && message.enabled)).toEqual([]);
});

test('pending backend startup leaves the first factual decoded playback free to grant readiness', () => {
  const view = join(false, 0, pendingStartup);
  expect(screen.getByRole('button', { name: 'Auto starting' })).toBeInTheDocument();
  expect(view.main().paused).toBe(false);
  expect(socket().sent.some((message) => message.type === 'playback' && message.available && !message.stalled && message.playing)).toBe(true);
  expect(socket().sent.filter((message) => message.type === 'auto')).toEqual([]);
});

test('captions start with Director Off and remain On when a later Director run is turned Off', () => {
  join(false, 0, directorOff);
  fireEvent.click(screen.getByRole('button', { name: 'Commentary captions Off' }));
  expect(socket().sent.filter((message) => message.type === 'captions')).toEqual([{ type: 'captions', session_id: id, enabled: true }]);
  expect(socket().sent.some((message) => message.type === 'auto')).toBe(false);
  act(() => socket().receive(snapshot(2, 0, { ...directorOff, captions: captionsOn })));
  fireEvent.click(screen.getByRole('button', { name: 'Auto off' }));
  act(() => socket().receive(snapshot(3, 0, { captions: captionsOn })));
  fireEvent.click(screen.getByRole('button', { name: 'Auto' }));
  act(() => socket().receive(snapshot(4, 0, { ...directorOff, captions: captionsOn })));
  expect(screen.getByRole('button', { name: 'Commentary captions On' })).toBeInTheDocument();
  expect(socket().sent.filter((message) => message.type === 'auto').map((message) => message.enabled)).toEqual([true, false]);
  expect(socket().sent.filter((message) => message.type === 'captions')).toHaveLength(1);
  act(() => socket().receive({ type: 'caption', session_id: id, sequence: 5,
    generation: 1, clock_epoch: 0, match_id: FEEDS[0].cameraId, source_key: FEEDS[0].sourceKey,
    source_identity: FEEDS[0].sourceIdentity, provider: 'elevenlabs', model: 'scribe_v2_realtime',
    state: 'committed', text: 'TEST independent caption.', source_anchor_seconds: 0 }));
  expect(screen.getByText('TEST independent caption.')).toBeInTheDocument();
});

test('turning captions Off leaves the active Director and native playback running', () => {
  const view = join(false, 0, { captions: captionsOn });
  fireEvent.click(screen.getByRole('button', { name: 'Commentary captions On' }));
  act(() => socket().receive(snapshot(2)));
  expect(screen.getByRole('button', { name: 'Auto' })).toHaveAttribute('aria-pressed', 'true');
  expect(view.main().paused).toBe(false);
  expect(socket().sent.filter((message) => message.type === 'auto')).toEqual([]);
  expect(socket().sent.filter((message) => message.type === 'captions')).toEqual([{ type: 'captions', session_id: id, enabled: false }]);
});

test('caption admission rejection remains visible while Off without changing Director or retrying', () => {
  join();
  fireEvent.click(screen.getByRole('button', { name: 'Commentary captions Off' }));
  act(() => { socket().receive({ type: 'error', feature: 'captions', code: 'rate_limited', retry_after: 42 });
    socket().receive(snapshot(2)); });
  expect(screen.getByRole('button', { name: 'Auto' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByRole('button', { name: 'Commentary captions Off' })).toBeInTheDocument();
  expect(screen.getByText(/10 Director or caption starts per hour.*42 seconds/)).toBeInTheDocument();
  act(() => jest.advanceTimersByTime(10000));
  expect(socket().sent.filter((message) => message.type === 'captions')).toHaveLength(1);
  expect(socket().sent.filter((message) => message.type === 'auto')).toEqual([]);
  fireEvent.click(screen.getByRole('button', { name: 'Commentary captions Off' }));
  expect(screen.queryByText(/42 seconds/)).not.toBeInTheDocument();
});

test('Director rejection leaves independently active captions and manual playback alone', () => {
  const view = join(false, 25, { ...directorOff, captions: captionsOn });
  fireEvent.click(screen.getByRole('button', { name: 'Auto off' }));
  act(() => socket().receive({ type: 'error', feature: 'director', code: 'budget_exhausted' }));
  expect(screen.getByRole('button', { name: 'Commentary captions On' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Auto off' })).toBeInTheDocument();
  expect(view.main().paused).toBe(false);
  expect(socket().sent.filter((message) => message.type === 'captions')).toEqual([]);
});

test('a finished Director run can be explicitly restarted for a fresh twenty-minute run', () => {
  join();
  act(() => socket().receive(snapshot(2, 0, { ...directorOff, state: 'stopped', ai_reason: 'ai_finished', ai_remaining_seconds: 0 })));
  expect(screen.getByRole('button', { name: 'Auto off' })).toBeEnabled();
  fireEvent.click(screen.getByRole('button', { name: 'Auto off' }));
  act(() => socket().receive(snapshot(3)));
  expect(screen.getByRole('button', { name: 'Auto' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByText('Director run: 1200s remaining.')).toBeInTheDocument();
  expect(socket().sent.filter((message) => message.type === 'auto')).toEqual([{ type: 'auto', session_id: id, enabled: true }]);
});
