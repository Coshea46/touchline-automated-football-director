import { act, fireEvent, render, screen } from '@testing-library/react';
import App from '../app/App';
import { FEEDS, getPreviewUrl, getVideoUrl } from '../features/watch/sources/feeds';
import { LIVE_CAPTION_MODEL } from '../features/captions/liveCaptions';

const MODEL = 'temporal-shots3_10-d63fff64480d/classifier.pt';
const POSITION = 37.25;
const OFFSET = 120;
const EPOCH = 3;
const socket = () => global.FakeWebSocket.instances.at(-1);
const receive = (value) => act(() => socket().receive(value));
const autoMessages = () => socket().sent.filter((message) => message.type === 'auto');

function envelope(overrides = {}) {
  return {
    type: 'session', protocol_version: 1, session_id: 'prepared-replay', sequence: 1,
    state: 'disabled', mode: 'disabled', model_version: 'disabled', server_time: Date.now() / 1000,
    controls_enabled: true, auto_enabled: false, auto_requested: false, auto_available: false,
    auto_can_start: true, auto_starting: false, auto_start_reason: 'available', manual_hold_until: 0,
    current_match_id: 'cam-1', playback: { position: POSITION, playing: true, rate: 1, clock_epoch: EPOCH },
    matches: FEEDS.map((feed) => ({ match_id: feed.cameraId, label: feed.label, url: getVideoUrl(feed),
      preview_url: getPreviewUrl(feed), source_key: feed.sourceKey, source_identity: feed.sourceIdentity,
      offset_seconds: OFFSET, healthy: true })),
    director: { mode: 'balanced', config: { cooldown_seconds: 12, staying_bonus: .2, confirmations_required: 2 },
      latest_scores: {}, scores_fresh: false, history: [] },
    captions: { available: true, configured: true, user_enabled: true, enabled: true, status: 'listening',
      model: LIVE_CAPTION_MODEL, generation: 1, match_id: 'cam-1', clock_epoch: EPOCH,
      deadline: Date.now() / 1000 + 30 },
    ...overrides,
  };
}

function starting(overrides = {}) {
  return envelope({ sequence: 2, mode: 'real', model_version: MODEL, state: 'starting',
    auto_enabled: false, auto_requested: true, auto_available: false,
    auto_can_start: false, auto_starting: true, auto_start_reason: 'starting', ...overrides });
}

function running(overrides = {}) {
  return envelope({ sequence: 4, mode: 'real', model_version: MODEL, state: 'running',
    auto_enabled: true, auto_requested: true, auto_available: true,
    auto_can_start: false, auto_starting: false, auto_start_reason: 'monitoring_active', ...overrides });
}

function ready(video) {
  Object.defineProperty(video, 'readyState', { configurable: true, value: 2 });
  Object.defineProperty(video, 'duration', { configurable: true, value: 1000 });
  fireEvent.loadedMetadata(video);
  fireEvent.canPlay(video);
}

function join() {
  const view = render(<App />);
  act(() => { socket().open(); socket().receive(envelope()); });
  const video = screen.getByLabelText(`${FEEDS[0].label} video`);
  ready(video);
  return { ...view, video };
}

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, '', '/');
});

test('onboarding warms from factual paused zero and opens only after tutorial plus Director readiness', () => {
  process.env.REACT_APP_STARTUP_TUTORIAL = 'true';
  jest.useFakeTimers();
  try {
    const { container } = render(<App />);
    const zero = { playback: { position: 0, playing: false, rate: 1, clock_epoch: 0 },
      matches: envelope().matches.map((feed) => ({ ...feed, offset_seconds: 0 })) };
    act(() => { socket().open(); socket().receive(envelope(zero)); });
    const main = container.querySelector('.tile--main video');
    ready(main);
    act(() => jest.advanceTimersByTime(500));
    expect(main.currentTime).toBe(0);
    expect(socket().sent.filter((m) => m.type === 'setup')).toHaveLength(1);
    expect(socket().sent.filter((m) => m.type === 'playback').every((m) => !m.playing && m.position === 0)).toBe(true);
    fireEvent.click(screen.getByText('Skip tutorial'));
    expect(screen.getByRole('heading', { name: 'Setting things up' })).toBeVisible();
    receive(running(zero));
    act(() => jest.advanceTimersByTime(350));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  } finally {
    delete process.env.REACT_APP_STARTUP_TUTORIAL;
    jest.useRealTimers();
  }
});

test('startup preserves the worker failure reason after cancellation closes the connection', () => {
  process.env.REACT_APP_STARTUP_TUTORIAL = 'true';
  jest.useFakeTimers();
  try {
    render(<App />);
    act(() => { socket().open(); socket().receive(envelope({ state: 'error', error_code: 'inference_unavailable' })); });
    expect(screen.getByRole('heading', { name: 'Setup paused' })).toBeVisible();
    expect(screen.getByText(/could not connect to its AI workers/)).toBeVisible();
    act(() => jest.advanceTimersByTime(120000));
    expect(screen.getByText(/could not connect to its AI workers/)).toBeVisible();
    expect(screen.queryByText(/startup took too long/)).not.toBeInTheDocument();
    expect(socket().readyState).toBe(3);
  } finally {
    delete process.env.REACT_APP_STARTUP_TUTORIAL;
    jest.useRealTimers();
  }
});

test('a prepared CPU session starts once through Auto and cannot apply a recommendation before readiness', () => {
  const { container, video } = join();
  const nodes = Array.from(container.querySelectorAll('video'));
  expect(nodes).toHaveLength(FEEDS.length);
  expect(container.querySelector('.tile--main video')).toBe(video);
  expect(video.currentTime).toBe(POSITION + OFFSET);
  const button = screen.getByRole('button', { name: 'Auto off' });
  expect(button).toBeEnabled();
  fireEvent.click(button);
  expect(autoMessages()).toEqual([{ type: 'auto', session_id: 'prepared-replay', enabled: true }]);

  receive(starting());
  expect(screen.getByRole('button', { name: 'Auto starting' })).toHaveTextContent(/Starting/);
  expect(screen.getByRole('button', { name: 'Auto starting' })).toHaveAttribute('aria-pressed', 'false');
  // A native canplay may arrive during the startup buffer. Readiness must still
  // reject a fresh, otherwise correctly identified recommendation independently.
  fireEvent.canPlay(video);
  receive({ type: 'recommendation', protocol_version: 1, session_id: 'prepared-replay', sequence: 3,
    model_version: MODEL, score_kind: 'model', clock_epoch: EPOCH, selected_match_id: 'cam-2',
    playback_time: POSITION, observation_end: POSITION, emitted_at: Date.now() / 1000,
    valid_until: Date.now() / 1000 + 3, latency_seconds: .2,
    scores: Object.fromEntries(FEEDS.map((feed) => [feed.cameraId, feed.cameraId === 'cam-2' ? .9 : .1])) });
  expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBe(video);
  expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);

  receive(running());
  expect(screen.getByRole('button', { name: 'Auto' })).toHaveAttribute('aria-pressed', 'true');
  expect(autoMessages()).toHaveLength(1);
  expect(video.currentTime).toBe(POSITION + OFFSET);
  Array.from(container.querySelectorAll('video')).forEach((node, index) => expect(node).toBe(nodes[index]));
});

test('Off during startup survives its acknowledgement and late readiness without re-enabling or restarting captions', () => {
  const { video } = join();
  const deadline = Date.now() / 1000 + 30;
  const captions = envelope().captions;
  fireEvent.click(screen.getByRole('button', { name: 'Auto off' }));
  receive(starting({ captions: { ...captions, deadline } }));
  const warming = screen.getByRole('button', { name: 'Auto starting' });
  expect(warming).toBeEnabled();
  fireEvent.click(warming);
  expect(autoMessages().map((message) => message.enabled)).toEqual([true, false]);

  receive(starting({ sequence: 3, auto_requested: false, captions: { ...captions, deadline } }));
  receive(running({ auto_requested: false, auto_enabled: false, captions: { ...captions, deadline } }));
  expect(screen.getByRole('button', { name: 'Auto off' })).toHaveAttribute('aria-pressed', 'false');
  expect(screen.queryByRole('button', { name: 'Auto' })).not.toBeInTheDocument();
  expect(autoMessages().map((message) => message.enabled)).toEqual([true, false]);
  expect(socket().sent.filter((message) => message.type === 'captions')).toHaveLength(0);
  expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBe(video);
  expect(video.currentTime).toBe(POSITION + OFFSET);

  // The existing provider allowance can expire while ML is warming. Its
  // authoritative terminal state must remain visible rather than restart On.
  receive(running({ sequence: 5, auto_requested: false, auto_enabled: false,
    captions: { ...captions, deadline, enabled: false, status: 'stopped', reason: 'budget_deadline' } }));
  expect(screen.getByText('Live caption session allowance has been reached.')).toBeInTheDocument();
  expect(socket().sent.filter((message) => message.type === 'captions')).toHaveLength(0);
});

test('ML readiness and unhealthy monitoring metadata preserve the held manual source, media nodes and paused source time', () => {
  const { container, video: outgoing } = join();
  const nodes = Array.from(container.querySelectorAll('video'));
  const video = container.querySelector(`.tile[data-feed-id="${FEEDS[2].id}"] video`);
  const source = video.getAttribute('src');
  expect(nodes).toHaveLength(FEEDS.length);
  expect(video).not.toBe(outgoing);
  fireEvent.click(screen.getByRole('button', { name: FEEDS[2].label }));
  expect(container.querySelector('.tile--main video')).toBe(video);
  expect(outgoing.closest('.tile')).toHaveClass('tile--preview');
  ready(video);
  video.playbackRate = 1.25;
  video.volume = .4;
  fireEvent.timeUpdate(video);
  fireEvent.pause(video);
  const hold = Date.now() / 1000 + 30;
  const caption = { ...envelope().captions, match_id: 'cam-3', generation: 2 };
  const play = jest.spyOn(video, 'play');
  play.mockClear();
  fireEvent.click(screen.getByRole('button', { name: 'Auto off' }));
  receive(starting({ current_match_id: 'cam-3', manual_hold_until: hold, captions: caption }));
  const matches = envelope().matches.map((feed) => ({ ...feed, healthy: feed.match_id !== 'cam-3' }));
  receive(running({ current_match_id: 'cam-3', manual_hold_until: hold, matches, captions: caption }));

  expect(screen.getByLabelText(`${FEEDS[2].label} video`)).toBe(video);
  expect(video.getAttribute('src')).toBe(source);
  expect(video.currentTime).toBe(POSITION + OFFSET);
  expect(video.playbackRate).toBe(1.25);
  expect(video.volume).toBe(.4);
  expect(play).not.toHaveBeenCalled();
  expect(screen.getByText(/Your selection/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Auto paused' })).toHaveAttribute('aria-pressed', 'true');
  expect(socket().sent.filter((message) => message.type === 'select').map((message) => message.match_id)).toEqual(['cam-3']);
  expect(socket().sent.filter((message) => message.type === 'captions')).toHaveLength(0);
  Array.from(container.querySelectorAll('video')).forEach((node, index) => expect(node).toBe(nodes[index]));
  play.mockRestore();
});
