import { render, screen, fireEvent, within } from '@testing-library/react';
import App from '../app/App';
import MainPlayer, { boundedTime } from '../features/watch/compatibility/MainPlayer';
import { FEEDS, getVideoUrl, getPreviewUrl } from '../features/watch/sources/feeds';

beforeEach(() => { localStorage.clear(); window.history.replaceState(null, '', '/'); });

function ready(video) {
  Object.defineProperty(video, 'readyState', { configurable: true, value: 2 });
  Object.defineProperty(video, 'duration', { configurable: true, value: 418.69 });
  fireEvent.loadedMetadata(video);
  fireEvent.canPlay(video);
}

const nativePlayer = (id) => document.querySelector(`[data-feed-id="${id}"] video`);

test('prioritizes the focused decoder before starting six previews on the same seven mounted players', () => {
  render(<App />);
  const main = screen.getByLabelText(`${FEEDS[0].label} video`);
  expect(main).toHaveAttribute('src', getVideoUrl(FEEDS[0]));
  expect(main).toHaveAttribute('controls');
  const strip = screen.getByRole('region', { name: 'Other matches' });
  expect(within(strip).getAllByRole('button')).toHaveLength(6);
  expect(strip.querySelectorAll('video')).toHaveLength(7);
  const nodes = [...strip.querySelectorAll('video')];
  nodes.slice(1).forEach((video) => expect(video).not.toHaveAttribute('src'));
  Object.defineProperty(main, 'readyState', { configurable: true, value: 1 });
  fireEvent.loadedMetadata(main);
  nodes.slice(1).forEach((video) => expect(video).not.toHaveAttribute('src'));
  ready(main);
  strip.querySelectorAll('video').forEach((video, i) => {
    expect(video).toBe(nodes[i]);
    expect(video).toHaveAttribute('src', getVideoUrl(FEEDS[i]));
  });
  expect(getVideoUrl(FEEDS[0])).toMatch(/\/match-1\.mp4$/);
  expect(getPreviewUrl(FEEDS[6])).toMatch(/\/match-7-preview-v1\.mp4$/);
});

test('a new focus loads immediately before initial readiness while already started decoders stay loaded', () => {
  const { container } = render(<App />);
  const nodes = [...container.querySelectorAll('video')];
  fireEvent.click(screen.getByRole('button', { name: FEEDS[2].label }));
  expect(screen.getByLabelText(`${FEEDS[2].label} video`)).toBe(nodes[2]);
  expect(nodes[0]).toHaveAttribute('src', getVideoUrl(FEEDS[0]));
  expect(nodes[2]).toHaveAttribute('src', getVideoUrl(FEEDS[2]));
  [1, 3, 4, 5, 6].forEach((index) => expect(nodes[index]).not.toHaveAttribute('src'));
  ready(nodes[2]);
  nodes.forEach((node, index) => {
    expect(container.querySelectorAll('video')[index]).toBe(node);
    expect(node).toHaveAttribute('src', getVideoUrl(FEEDS[index]));
  });
});

test('switching promotes the existing destination video without replacing any player', () => {
  const { container } = render(<App />);
  const main = screen.getByLabelText(`${FEEDS[0].label} video`);
  ready(main);
  main.currentTime = 125;
  fireEvent.timeUpdate(main);
  const destination = container.querySelector('[data-feed-id="3"] video');
  ready(destination);
  const videos = Array.from(container.querySelectorAll('video'));
  fireEvent.click(screen.getByRole('button', { name: FEEDS[2].label }));
  expect(screen.getByLabelText(`${FEEDS[2].label} video`)).toBe(destination);
  expect(destination).not.toBe(main);
  expect(destination.currentTime).toBe(125);
  Array.from(container.querySelectorAll('video')).forEach((video, index) => expect(video).toBe(videos[index]));
  expect(screen.getByRole('button', { name: FEEDS[0].label })).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: FEEDS[2].label })).toHaveFocus();
});

test('a genuinely different main source restores timeline, pause, rate and volume', () => {
  const clock = { current: { time: 0, playing: true, rate: 1, muted: false, volume: 1 } };
  const { rerender } = render(<MainPlayer feed={{ id: 1, label: 'A', mainKey: 'a.mp4' }} clock={clock} />);
  const video = screen.getByLabelText('A video');
  ready(video);
  video.currentTime = 82;
  video.playbackRate = 1.5;
  video.volume = 0.4;
  video.muted = true;
  fireEvent.timeUpdate(video);
  fireEvent.pause(video);
  const play = jest.spyOn(video, 'play');
  play.mockClear();
  rerender(<MainPlayer feed={{ id: 2, label: 'B', mainKey: 'b.mp4' }} clock={clock} />);
  expect(screen.getByLabelText('B video')).toBe(video);
  video.currentTime = 0; // Simulate the browser resetting time for a new resource.
  ready(video);
  expect(video.currentTime).toBe(82);
  expect(video.playbackRate).toBe(1.5);
  expect(video.volume).toBe(0.4);
  expect(video.muted).toBe(true);
  expect(play).not.toHaveBeenCalled();
  play.mockRestore();
});

test('audio ownership changes mute without resetting playback or the user preference', () => {
  const clock = { current: { time: 29, playing: true, rate: 1.25, muted: false, volume: .4 } };
  const feed = { id: 1, cameraId: 'cam-1', label: 'First', mainKey: 'first.mp4', offsetSeconds: 120 };
  const { rerender } = render(<MainPlayer feed={feed} clock={clock} audioAllowed />);
  const video = screen.getByLabelText('First video');
  ready(video);
  const source = video.getAttribute('src');
  const play = jest.spyOn(video, 'play');
  const pause = jest.spyOn(video, 'pause');
  play.mockClear(); pause.mockClear();
  rerender(<MainPlayer feed={feed} clock={clock} audioAllowed={false} />);
  fireEvent.volumeChange(video);
  expect(video.muted).toBe(true);
  expect(clock.current.muted).toBe(false);
  expect(video.currentTime).toBe(149);
  expect(video.playbackRate).toBe(1.25);
  expect(video.getAttribute('src')).toBe(source);
  rerender(<MainPlayer feed={feed} clock={clock} audioAllowed />);
  expect(video.muted).toBe(false);
  expect(video.currentTime).toBe(149);
  expect(play).not.toHaveBeenCalled();
  expect(pause).not.toHaveBeenCalled();
  play.mockRestore(); pause.mockRestore();
});

test('a changed source resumes from its first decoded frame without requiring canplay', () => {
  const clock = { current: { time: 29.015997, playing: true, rate: 1, muted: false, volume: .4 } };
  const availability = jest.fn();
  const first = { id: 1, cameraId: 'cam-1', label: 'First', mainKey: 'first.mp4', offsetSeconds: 120 };
  const second = { id: 2, cameraId: 'cam-2', label: 'Second', mainKey: 'second.mp4', offsetSeconds: 120 };
  const { rerender } = render(<MainPlayer feed={first} clock={clock} onAvailability={availability} />);
  const video = screen.getByLabelText('First video');
  ready(video);
  const play = jest.spyOn(video, 'play');
  play.mockClear();
  availability.mockClear();
  rerender(<MainPlayer feed={second} clock={clock} onAvailability={availability} />);
  video.currentTime = 0; // The browser resets its native clock for the new resource.
  Object.defineProperty(video, 'readyState', { configurable: true, value: 1 });
  fireEvent.loadedMetadata(video);
  expect(video.currentTime).toBeCloseTo(149.015997, 6);
  expect(clock.current.time).toBeCloseTo(29.015997, 6);
  expect(screen.getByText('Loading match...')).toBeInTheDocument();
  expect(play).not.toHaveBeenCalled();

  Object.defineProperty(video, 'readyState', { configurable: true, value: 2 });
  Object.defineProperty(video, 'seeking', { configurable: true, value: true });
  fireEvent.loadedData(video);
  expect(screen.getByText('Loading match...')).toBeInTheDocument();
  expect(availability).not.toHaveBeenCalled();
  Object.defineProperty(video, 'seeking', { configurable: true, value: false });
  Object.defineProperty(video, 'readyState', { configurable: true, value: 1 });
  fireEvent.loadedData(video);
  expect(play).not.toHaveBeenCalled();
  expect(availability).not.toHaveBeenCalled();

  Object.defineProperty(video, 'readyState', { configurable: true, value: 2 });
  fireEvent.loadedData(video); // No canplay or seeked event is required.
  expect(screen.queryByText('Loading match...')).not.toBeInTheDocument();
  expect(availability).toHaveBeenLastCalledWith('cam-2', true);
  expect(video.matchpulseReadySource).toBe(`${getVideoUrl(second)}\ncam-2`);
  expect(video.currentTime).toBeCloseTo(149.015997, 6);
  expect(clock.current.stalled).toBe(false);
  expect(play).toHaveBeenCalledTimes(1);
  play.mockRestore();
});

test('Space and scoped number shortcuts continue to select matches', () => {
  render(<App />);
  fireEvent.keyDown(window, { key: '4' });
  expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBeInTheDocument();
  fireEvent.keyDown(screen.getByRole('button', { name: FEEDS[1].label }), { key: ' ' });
  expect(screen.getByLabelText(`${FEEDS[1].label} video`)).toBeInTheDocument();
  fireEvent.keyDown(screen.getByRole('heading', { name: FEEDS[1].label }), { key: '4' });
  expect(screen.getByLabelText(`${FEEDS[3].label} video`)).toBeInTheDocument();
  fireEvent.keyDown(screen.getByRole('heading', { name: FEEDS[3].label }), { key: 'Escape' });
  expect(screen.getByRole('button', { name: FEEDS[0].label })).toHaveFocus();
});

test('pause previews never pauses the main element, and selection keeps previews paused', () => {
  const pause = jest.spyOn(window.HTMLMediaElement.prototype, 'pause').mockImplementation(function stop() {
    Object.defineProperty(this, 'paused', { configurable: true, value: true });
  });
  const { container } = render(<App />);
  const main = screen.getByLabelText(`${FEEDS[0].label} video`);
  const previews = Array.from(container.querySelectorAll('.tile--preview video'));
  container.querySelectorAll('video').forEach((video) => {
    ready(video);
    Object.defineProperty(video, 'paused', { configurable: true, value: false });
  });
  pause.mockClear();
  fireEvent.click(screen.getByRole('button', { name: 'Pause previews' }));
  expect(new Set(pause.mock.instances)).toEqual(new Set(previews));
  expect(pause.mock.instances).not.toContain(main);
  pause.mockClear();
  fireEvent.click(screen.getByRole('button', { name: FEEDS[2].label }));
  expect(screen.getByRole('button', { name: 'Resume previews' })).toHaveAttribute('aria-pressed', 'true');
  expect(pause.mock.instances).not.toContain(screen.getByLabelText(`${FEEDS[2].label} video`));
  pause.mockRestore();
});

test('a returning preview seeks to the main timeline without being recreated', () => {
  const { container } = render(<App />);
  const main = screen.getByLabelText(`${FEEDS[0].label} video`);
  const preview = container.querySelector('[data-feed-id="1"] video');
  ready(main);
  ready(preview);
  main.currentTime = 100;
  fireEvent.timeUpdate(main);
  fireEvent.click(screen.getByRole('button', { name: FEEDS[2].label }));
  expect(container.querySelector('[data-feed-id="1"] video')).toBe(preview);
  expect(preview.currentTime).toBe(100);
});

test('light is the default and saved dark mode persists without replacing media elements', () => {
  const { unmount } = render(<App />);
  const main = screen.getByLabelText(`${FEEDS[0].label} video`);
  expect(document.documentElement).toHaveAttribute('data-theme', 'light');
  fireEvent.click(screen.getByRole('button', { name: 'Switch to dark mode' }));
  expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBe(main);
  expect(localStorage.getItem('match-switcher-theme')).toBe('dark');
  unmount();
  render(<App />);
  expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
  fireEvent.click(screen.getByRole('button', { name: 'Switch to light mode' }));
  expect(document.documentElement).toHaveAttribute('data-theme', 'light');
});

test('main failures expose retry and background full-source failures are visible', () => {
  const { container } = render(<App />);
  fireEvent.error(screen.getByLabelText(`${FEEDS[0].label} video`));
  expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  const preview = container.querySelector('[data-feed-id="2"] video');
  expect(preview).toHaveAttribute('src', getVideoUrl(FEEDS[1]));
  fireEvent.error(preview);
  expect(screen.getByText('Match unavailable')).toBeInTheDocument();
  const load = jest.spyOn(preview, 'load').mockImplementation(() => {});
  fireEvent.click(within(preview.closest('.tile')).getByRole('button', { name: 'Retry' }));
  expect(load).toHaveBeenCalledTimes(1);
  expect(preview).toHaveAttribute('src', getVideoUrl(FEEDS[1]));
  expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBeInTheDocument();
  load.mockRestore();
});

test('seek targets remain within the destination duration', () => {
  expect(boundedTime(500, 100)).toBe(99.9);
  expect(boundedTime(-10, 100)).toBe(0);
  expect(boundedTime(25, NaN)).toBe(25);
  expect(boundedTime(99.98, 100)).toBe(99.98);
});

test('an exhausted destination reports unavailable without rewinding the common replay clock', () => {
  const clock = { current: { time: 0, playing: true, rate: 1, muted: false, volume: 1 } };
  const availability = jest.fn();
  const { rerender } = render(<MainPlayer feed={{ id: 1, cameraId: 'cam-1', label: 'A', mainKey: 'a.mp4' }}
    clock={clock} onAvailability={availability} />);
  const video = screen.getByLabelText('A video');
  ready(video);
  video.currentTime = 125;
  fireEvent.timeUpdate(video);
  rerender(<MainPlayer feed={{ id: 2, cameraId: 'cam-2', label: 'B', mainKey: 'b.mp4' }}
    clock={clock} onAvailability={availability} />);
  video.currentTime = 0;
  Object.defineProperty(video, 'duration', { configurable: true, value: 100 });
  fireEvent.loadedMetadata(video);
  fireEvent.canPlay(video);
  expect(clock.current.time).toBe(125);
  expect(clock.current.stalled).toBe(true);
  expect(clock.current.available).toBe(false);
  expect(availability).toHaveBeenLastCalledWith('cam-2', false);
  expect(screen.getByText('Video unavailable.')).toBeInTheDocument();
});

describe('backend WebSocket', () => {
  const { act } = require('@testing-library/react');
  const { feedIdFromTarget } = require('../features/watch/sources/feeds');
  const socket = () => global.FakeWebSocket.instances.at(-1);
  const send = (message) => act(() => { socket().receive(message); });
  const director = (overrides = {}) => ({
    mode: 'balanced', config: { cooldown_seconds: 12, staying_bonus: .2, confirmations_required: 2 },
    selection_kind: 'initial', selection_reason: 'initial_selection', decision_reason: 'waiting_for_scores',
    last_switch_at: null, auto_cooldown_until: null, latest_scores: { 'cam-1': .1, 'cam-3': .8 },
    scores_observation_end: 100, scores_produced_at: Date.now() / 1000, scores_valid_until: Date.now() / 1000 + 3,
    scores_fresh: true, history: [], ...overrides,
  });
  const session = (overrides = {}) => ({
    type: 'session', protocol_version: 1, session_id: 'replay-1', sequence: 1,
    state: 'running', mode: 'features_mock', model_version: 'mock-v1', server_time: Date.now() / 1000,
    controls_enabled: true, auto_enabled: true, manual_hold_until: 0,
    playback: { position: 100, playing: true, rate: 1, clock_epoch: 0 }, current_match_id: 'cam-1',
    matches: FEEDS.map((feed) => ({ match_id: feed.cameraId, label: feed.label,
      url: getVideoUrl(feed), offset_seconds: 0, healthy: true })), director: director(), ...overrides,
  });
  const recommend = (overrides = {}) => ({
    type: 'recommendation', protocol_version: 1, session_id: 'replay-1', sequence: 2,
    model_version: 'mock-v1', score_kind: 'mock', clock_epoch: 0,
    selected_match_id: 'cam-3', playback_time: 100, observation_end: 100,
    emitted_at: Date.now() / 1000, valid_until: Date.now() / 1000 + 3,
    scores: Object.fromEntries(FEEDS.map((feed) => [feed.cameraId, feed.id === 3 ? 0.8 : 0.1])), latency_seconds: 0.2, ...overrides,
  });
  const join = (overrides, prewarm = true) => {
    act(() => socket().open());
    send(session(overrides));
    if (prewarm) document.querySelectorAll('.persistent-player video').forEach(ready);
    else ready(screen.getByLabelText(`${FEEDS[0].label} video`));
  };

  test('maps backend feed names to matches', () => {
    expect(feedIdFromTarget('cam-3')).toBe(3);
    expect(feedIdFromTarget('3')).toBe(3);
    expect(feedIdFromTarget(7)).toBe(7);
    expect(feedIdFromTarget('cam-9')).toBeNull();
    expect(feedIdFromTarget(undefined)).toBeNull();
  });

  test('connects to the backend and keeps factual monitoring state beside the director', () => {
    render(<App />);
    expect(socket().url).toBe('ws://127.0.0.1:8030');
    expect(within(screen.getByRole('banner')).queryByText(/^(Connecting|Connected|Offline)$/)).not.toBeInTheDocument();
    join();
    expect(within(screen.getByRole('complementary', { name: 'Auto Director settings' }))
      .getByText('Monitoring 7 matches')).toBeInTheDocument();
  });

  test('Auto is unavailable before a ready session while manual playback remains usable', () => {
    render(<App />);
    const auto = screen.getByRole('button', { name: 'Auto unavailable' });
    expect(auto).toBeDisabled();
    expect(auto).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(auto);
    expect(socket().sent.filter((message) => message.type === 'auto')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: FEEDS[1].label }));
    expect(screen.getByLabelText(`${FEEDS[1].label} video`)).toBeInTheDocument();
  });

  test('finite monitoring expiry keeps the website connected and manual playback available', () => {
    render(<App />);
    join();
    const video = screen.getByLabelText(`${FEEDS[0].label} video`);
    const before = video.currentTime;
    send(session({ sequence: 2, state: 'stopped', auto_available: false, auto_enabled: false,
      error_code: 'session_limit_reached' }));
    expect(screen.getByText('Monitoring session ended')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Auto unavailable' })).toBeDisabled();
    expect(screen.queryByText(/Director offline/)).not.toBeInTheDocument();
    expect(socket().readyState).toBe(1);
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBe(video);
    expect(video.currentTime).toBe(before);
    fireEvent.click(screen.getByRole('button', { name: FEEDS[1].label }));
    expect(screen.getByLabelText(`${FEEDS[1].label} video`)).toBe(nativePlayer(2));
    expect(nativePlayer(2)).not.toBe(video);
    expect(socket().sent.some((message) => message.type === 'select' && message.match_id === 'cam-2')).toBe(true);
  });

  test('the selected real model becomes Auto On only after readiness without changing the player', () => {
    const model = 'temporal-shots3_10-d63fff64480d/classifier.pt';
    const { container } = render(<App />);
    join({ mode: 'real', model_version: model, state: 'starting', auto_available: false,
      auto_requested: true, auto_enabled: false });
    const video = screen.getByLabelText(`${FEEDS[0].label} video`);
    const media = Array.from(container.querySelectorAll('video'));
    expect(screen.getByRole('button', { name: 'Auto unavailable' })).toBeDisabled();
    send(session({ mode: 'real', model_version: model, sequence: 2, state: 'running',
      auto_available: true, auto_requested: true, auto_enabled: true }));
    expect(screen.getByRole('button', { name: 'Auto' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Auto' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('combobox', { name: 'Director mode' })).toHaveValue('balanced');
    expect(screen.getByText('Monitoring 7 matches')).toHaveAttribute('title', `Active model: ${model}`);
    expect(video.currentTime).toBe(100);
    expect(Array.from(container.querySelectorAll('video'))).toEqual(media);
    expect(socket().sent.filter((message) => message.type === 'auto')).toHaveLength(0);
    send(recommend({ sequence: 3, model_version: model, score_kind: 'model' }));
    expect(screen.getByLabelText(`${FEEDS[2].label} video`)).toBe(nativePlayer(3));
    expect(nativePlayer(3)).not.toBe(video);
    expect(socket().sent).toContainEqual(expect.objectContaining({ type: 'ack', sequence: 3, match_id: 'cam-3' }));
  });

  test('an explicit unavailable model overrides active-state compatibility and never applies scores', () => {
    render(<App />);
    join({ state: 'running', auto_available: false, auto_enabled: false, auto_requested: true });
    expect(screen.getByRole('button', { name: 'Auto unavailable' })).toBeDisabled();
    send(recommend());
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBeInTheDocument();
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
  });

  test('an acknowledged Auto Off survives readiness updates and On resumes using fresh scores', () => {
    render(<App />);
    join({ auto_available: true, auto_requested: true, auto_enabled: true });
    const video = screen.getByLabelText(`${FEEDS[0].label} video`);
    fireEvent.click(screen.getByRole('button', { name: 'Auto' }));
    expect(socket().sent).toContainEqual({ type: 'auto', session_id: 'replay-1', enabled: false });
    send(session({ sequence: 2, auto_available: true, auto_requested: false, auto_enabled: false }));
    expect(screen.getByRole('button', { name: 'Auto off' })).toHaveAttribute('aria-pressed', 'false');
    send(recommend({ sequence: 3 }));
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBe(video);
    fireEvent.click(screen.getByRole('button', { name: 'Auto off' }));
    expect(socket().sent).toContainEqual({ type: 'auto', session_id: 'replay-1', enabled: true });
    send(session({ sequence: 4, auto_available: true, auto_requested: true, auto_enabled: true }));
    send(recommend({ sequence: 5 }));
    expect(screen.getByLabelText(`${FEEDS[2].label} video`)).toBe(nativePlayer(3));
    expect(nativePlayer(3).currentTime).toBe(100);
  });

  test('losing the backend disables Auto without blocking a manual source change', () => {
    render(<App />);
    join(); // An older active envelope without auto_available retains compatibility.
    expect(screen.getByRole('button', { name: 'Auto' })).toHaveAttribute('aria-pressed', 'true');
    act(() => socket().drop());
    expect(screen.getByRole('button', { name: 'Auto unavailable' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Auto unavailable' })).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(screen.getByRole('button', { name: FEEDS[1].label }));
    expect(screen.getByLabelText(`${FEEDS[1].label} video`)).toBeInTheDocument();
  });

  test('a fresh recommendation changes the main match without stealing focus', () => {
    render(<App />);
    join();
    const toggle = screen.getByRole('button', { name: 'Auto' });
    toggle.focus();
    send(recommend());
    expect(screen.getByLabelText(`${FEEDS[2].label} video`)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: FEEDS[2].label })).not.toHaveFocus();
    expect(toggle).toHaveFocus();
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(1);
    expect(screen.getByText(`Switched to ${FEEDS[2].label}`)).toBeInTheDocument();
    expect(screen.getByText('Mock scores')).toBeInTheDocument();
    expect(socket().sent).toContainEqual(expect.objectContaining({ type: 'ack', session_id: 'replay-1', sequence: 2,
      clock_epoch: 0, match_id: 'cam-3', received_at: expect.any(Number) }));
    expect(socket().sent.at(-1)).toMatchObject({ type: 'playback', match_id: 'cam-3', position: 100 });
  });

  test('cold automatic proposals are discarded and only a fresh ready proposal can switch and ACK', () => {
    render(<App />);
    join(undefined, false);
    const video = screen.getByLabelText(`${FEEDS[0].label} video`);
    const destination = nativePlayer(3);
    send(recommend());
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBe(video);
    Object.defineProperty(destination, 'readyState', { configurable: true, value: 1 });
    Object.defineProperty(destination, 'duration', { configurable: true, value: 418.69 });
    fireEvent.loadedMetadata(destination);
    expect(destination.currentTime).toBe(100);
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
    Object.defineProperty(destination, 'readyState', { configurable: true, value: 2 });
    Object.defineProperty(destination, 'seeking', { configurable: true, value: true });
    fireEvent.loadedData(destination);
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
    Object.defineProperty(destination, 'seeking', { configurable: true, value: false });
    Object.defineProperty(destination, 'readyState', { configurable: true, value: 1 });
    fireEvent.loadedData(destination);
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
    Object.defineProperty(destination, 'readyState', { configurable: true, value: 2 });
    fireEvent.loadedData(destination);
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBe(video);
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
    send(recommend({ sequence: 3 }));
    expect(screen.getByLabelText(`${FEEDS[2].label} video`)).toBe(destination);
    expect(socket().sent.filter((message) => message.type === 'ack')).toEqual([
      expect.objectContaining({ sequence: 3, match_id: 'cam-3', clock_epoch: 0 }),
    ]);
    expect(destination.matchpulseReadySource).toBe(`${getVideoUrl(FEEDS[2])}\ncam-3`);
    expect(destination.closest('.tile')).toHaveAttribute('aria-busy', 'false');
    expect(socket().sent.at(-1)).toMatchObject({ type: 'playback', match_id: 'cam-3', position: 100 });
  });

  test('unknown feeds and malformed messages are ignored', () => {
    render(<App />);
    join();
    send(recommend({ selected_match_id: 'cam-42' }));
    send({ type: 'switch', target_feed: 'cam-3' }); // Obsolete unvalidated protocol cannot switch.
    act(() => socket().receive('not json'));
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBeInTheDocument();
  });

  test('a manual pick holds off automatic switches for 30 seconds', () => {
    jest.useFakeTimers();
    try {
      render(<App />);
      join();
      fireEvent.click(screen.getByRole('button', { name: FEEDS[1].label }));
      ready(screen.getByLabelText(`${FEEDS[1].label} video`));
      expect(screen.getByRole('button', { name: 'Auto paused' })).toBeInTheDocument();
      expect(socket().sent).toContainEqual({ type: 'select', session_id: 'replay-1', match_id: 'cam-2', manual_hold_seconds: 30 });
      send(recommend({ selected_match_id: 'cam-4' }));
      expect(screen.getByLabelText(`${FEEDS[1].label} video`)).toBeInTheDocument();
      act(() => { jest.advanceTimersByTime(30001); });
      expect(screen.getByRole('button', { name: 'Auto' })).toBeInTheDocument();
      // The held message is consumed, never queued or replayed after the hold.
      send(recommend({ selected_match_id: 'cam-4' }));
      expect(screen.getByLabelText(`${FEEDS[1].label} video`)).toBeInTheDocument();
      send(recommend({ sequence: 3, selected_match_id: 'cam-4' }));
      expect(screen.getByLabelText(`${FEEDS[3].label} video`)).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  test('turning Auto off ignores the backend', () => {
    render(<App />);
    join();
    fireEvent.click(screen.getByRole('button', { name: 'Auto' }));
    expect(screen.getByRole('button', { name: 'Auto off' })).toHaveAttribute('aria-pressed', 'false');
    expect(socket().sent).toContainEqual({ type: 'auto', session_id: 'replay-1', enabled: false });
    send(recommend({ selected_match_id: 'cam-5' }));
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBeInTheDocument();
  });

  test('initial session syncs once and reconnect preserves the actual viewer clock', () => {
    jest.useFakeTimers();
    try {
    render(<App />);
    const main = screen.getByLabelText(`${FEEDS[0].label} video`);
    ready(main);
    join();
    expect(main.currentTime).toBe(100);
    main.currentTime = 200;
    fireEvent.timeUpdate(main);
    act(() => { socket().drop(); });
    act(() => { jest.advanceTimersByTime(1000); socket().open(); });
    send(session({ sequence: 10, playback: { position: 0, playing: true, rate: 1, clock_epoch: 1 } }));
    expect(main.currentTime).toBe(200);
    expect(socket().sent.at(-1)).toMatchObject({ type: 'playback', position: 200 });
    } finally { jest.useRealTimers(); }
  });

  test('joining a nonzero session cannot grant future clock permission before the actual media seek', () => {
    jest.useFakeTimers();
    try {
      render(<App />);
      act(() => socket().open());
      send(session());
      act(() => jest.advanceTimersByTime(250));
      expect(socket().sent.at(-1)).toMatchObject({ type: 'playback', position: 0, stalled: true });
      ready(screen.getByLabelText(`${FEEDS[0].label} video`));
      expect(socket().sent.at(-1)).toMatchObject({ type: 'playback', position: 100 });
    } finally { jest.useRealTimers(); }
  });

  test('rejects old sessions, models, epochs, expired, future and out-of-order recommendations', () => {
    render(<App />);
    join();
    [
      { session_id: 'old' }, { model_version: 'old' }, { clock_epoch: 1 },
      { valid_until: Date.now() / 1000 - 1 }, { playback_time: 90, observation_end: 90 },
      { observation_end: 100.1 }, { playback_time: 110 }, { score_kind: 'model' }, { protocol_version: 2 },
    ].forEach((bad) => { send(recommend(bad)); expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBeInTheDocument(); });
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
    send(recommend({ sequence: 5 }));
    ready(screen.getByLabelText(`${FEEDS[2].label} video`));
    send(recommend({ sequence: 4, selected_match_id: 'cam-5' }));
    expect(screen.getByLabelText(`${FEEDS[2].label} video`)).toBeInTheDocument();
  });

  test('reports pause, buffering, seeks, resume and the 250ms actual playback clock', () => {
    jest.useFakeTimers();
    try {
      render(<App />);
      join();
      const video = screen.getByLabelText(`${FEEDS[0].label} video`);
      fireEvent.pause(video);
      expect(socket().sent.at(-1)).toMatchObject({ type: 'playback', position: 100, playing: false });
      fireEvent.waiting(video);
      expect(socket().sent.at(-1)).toMatchObject({ stalled: true });
      video.currentTime = 140;
      fireEvent.seeking(video);
      expect(socket().sent.at(-1)).toMatchObject({ position: 140, stalled: true });
      fireEvent.seeked(video);
      fireEvent.play(video);
      expect(socket().sent.at(-1)).toMatchObject({ position: 140, playing: true, stalled: false });
      video.currentTime = 140.2;
      fireEvent.timeUpdate(video);
      act(() => jest.advanceTimersByTime(250));
      expect(socket().sent.at(-1)).toMatchObject({ position: 140.2 });
      fireEvent.ended(video);
      expect(socket().sent.at(-1)).toMatchObject({ playing: false, ended: true });
      expect(video).not.toHaveAttribute('loop');
    } finally { jest.useRealTimers(); }
  });

  test('holds the replay during GPU startup and resumes when the worker is ready', () => {
    render(<App />);
    join({ state: 'starting' });
    const video = screen.getByLabelText(`${FEEDS[0].label} video`);
    const play = jest.spyOn(video, 'play');
    expect(screen.getByText('Preparing 7 matches')).toBeInTheDocument();
    expect(socket().sent.at(-1)).toMatchObject({ position: 100, playing: false });
    send(session({ state: 'running', sequence: 2 }));
    expect(play).toHaveBeenCalledTimes(1);
    expect(video.currentTime).toBe(100);
    expect(socket().sent.at(-1)).toMatchObject({ playing: true, position: 100 });
    play.mockRestore();
  });

  test('an offline backend releases the startup buffer for ordinary video playback', () => {
    render(<App />);
    join({ state: 'starting' });
    const video = screen.getByLabelText(`${FEEDS[0].label} video`);
    const play = jest.spyOn(video, 'play');
    act(() => socket().drop());
    expect(screen.getByText(/Director offline/)).toBeInTheDocument();
    expect(play).toHaveBeenCalledTimes(1);
    expect(video.currentTime).toBe(100);
    play.mockRestore();
  });

  test('ready automatic switches apply immediately without queuing browser view transitions', () => {
    document.startViewTransition = jest.fn(() => ({}));
    try {
      render(<App />);
      join();
      send(recommend());
      expect(screen.getByLabelText(`${FEEDS[2].label} video`)).toBe(nativePlayer(3));
      send(recommend({ sequence: 3, selected_match_id: 'cam-1' }));
      expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBeInTheDocument();
      expect(socket().sent.filter((message) => message.type === 'ack').map((message) => message.sequence)).toEqual([2, 3]);
      expect(document.startViewTransition).not.toHaveBeenCalled();
    } finally { delete document.startViewTransition; }
  });

  test('an immediate manual promotion survives its backend acknowledgement without view transitions', () => {
    document.startViewTransition = jest.fn(() => ({}));
    try {
      render(<App />);
      join();
      fireEvent.click(screen.getByRole('button', { name: FEEDS[1].label }));
      send(session({ sequence: 2, current_match_id: 'cam-2', manual_hold_until: Date.now() / 1000 + 30 }));
      expect(screen.getByLabelText(`${FEEDS[1].label} video`)).toBe(nativePlayer(2));
      expect(screen.getByRole('heading', { name: FEEDS[1].label })).toHaveFocus();
      expect(document.startViewTransition).not.toHaveBeenCalled();
    } finally { delete document.startViewTransition; }
  });

  test('an epoch change cannot resurrect a discarded cold-target proposal', () => {
    render(<App />);
    join(undefined, false);
    send(recommend());
    send(session({ sequence: 3, playback: { position: 100, playing: true, rate: 1, clock_epoch: 1 } }));
    ready(nativePlayer(3));
    send(recommend({ sequence: 4, clock_epoch: 0 }));
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBeInTheDocument();
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
    send(recommend({ sequence: 5, clock_epoch: 1 }));
    expect(screen.getByLabelText(`${FEEDS[2].label} video`)).toBe(nativePlayer(3));
    expect(socket().sent.filter((message) => message.type === 'ack').map((message) => message.sequence)).toEqual([5]);
  });

  test('instant promotion samples the current native clock even before its next timeupdate', () => {
    render(<App />);
    join();
    nativePlayer(1).currentTime = 100.75;
    nativePlayer(3).currentTime = 100.75;
    send(recommend({ observation_end: 99 }));
    expect(screen.getByLabelText(`${FEEDS[2].label} video`)).toBe(nativePlayer(3));
    expect(nativePlayer(3).currentTime).toBe(100.75);
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(1);
    expect(socket().sent.at(-1)).toMatchObject({ type: 'playback', position: 100.75 });
  });

  test('a promoted player keeps publishing its advancing native clock without timeupdate events', () => {
    jest.useFakeTimers();
    try {
      render(<App />);
      join();
      const nodes = [...document.querySelectorAll('.persistent-player video')];
      send(recommend());
      const promoted = nativePlayer(3);
      expect(screen.getByLabelText(`${FEEDS[2].label} video`)).toBe(promoted);
      for (const position of [101, 120, 190]) {
        promoted.currentTime = position;
        act(() => jest.advanceTimersByTime(250));
        expect(socket().sent.filter((message) => message.type === 'playback').at(-1))
          .toMatchObject({ match_id: 'cam-3', position, available: true, stalled: false });
      }
      nodes.forEach((node, index) => expect(document.querySelectorAll('.persistent-player video')[index]).toBe(node));
    } finally { jest.useRealTimers(); }
  });

  test('a stale observation is rejected using the current native clock before promotion', () => {
    render(<App />);
    join();
    nativePlayer(1).currentTime = 102.1;
    nativePlayer(3).currentTime = 102.1;
    send(recommend({ observation_end: 99 }));
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBeInTheDocument();
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
  });

  test('selecting the current match holds off a previously discarded cold-target recommendation', () => {
    render(<App />);
    join(undefined, false);
    send(recommend());
    fireEvent.keyDown(screen.getByRole('heading', { name: FEEDS[0].label }), { key: '1' });
    ready(nativePlayer(3));
    send(recommend({ sequence: 3 }));
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Auto paused' })).toBeInTheDocument();
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
  });

  test('monitoring-unavailable feeds remain manually playable but cannot be automatically selected', () => {
    render(<App />);
    const matches = session().matches.map((match) => ({ ...match, healthy: match.match_id !== 'cam-3' }));
    join({ matches });
    const missing = screen.getByRole('button', { name: `${FEEDS[2].label} (monitoring unavailable)` });
    send(recommend());
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBeInTheDocument();
    fireEvent.click(missing);
    expect(screen.getByLabelText(`${FEEDS[2].label} video`)).toBeInTheDocument();
  });

  test('registered sources and offsets replace guessed bucket paths without replacing the main DOM', () => {
    render(<App />);
    const video = screen.getByLabelText(`${FEEDS[0].label} video`);
    const matches = session().matches.slice(0, 2).map((match, i) => ({ ...match,
      url: `https://media.example/match-${i}.mp4?authorized=yes`, offset_seconds: 10 + i * 5 }));
    join({ matches });
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBe(video);
    expect(video).toHaveAttribute('src', matches[0].url);
    expect(video.currentTime).toBe(110);
    expect(socket().sent.at(-1)).toMatchObject({ position: 100 });
    video.currentTime = 152;
    fireEvent.timeUpdate(video);
    fireEvent.click(screen.getByRole('button', { name: FEEDS[1].label }));
    const destination = nativePlayer(2);
    expect(destination).not.toBe(video);
    ready(destination);
    expect(destination.currentTime).toBe(157);
    expect(socket().sent.at(-1)).toMatchObject({ match_id: 'cam-2', position: 142 });
  });

  test('secondary viewers receive state without controlling the shared backend clock', () => {
    render(<App />);
    join({ controls_enabled: false });
    expect(within(screen.getByRole('complementary', { name: 'Auto Director settings' }))
      .getByText(/Viewing only/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: FEEDS[1].label }));
    fireEvent.click(screen.getByRole('button', { name: 'Auto paused' }));
    expect(socket().sent).toHaveLength(0);
  });

  test('monitoring disabled still registers configured sources without pausing playback or accepting predictions', () => {
    render(<App />);
    const match = { ...session().matches[0], url: 'https://media.example/demo.mp4' };
    join({ state: 'disabled', mode: 'disabled', model_version: 'disabled', matches: [match] });
    expect(screen.getByText('Monitoring off')).toBeInTheDocument();
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toHaveAttribute('src', match.url);
    expect(socket().sent.at(-1)).toMatchObject({ playing: true });
    send(recommend({ model_version: 'disabled' }));
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
  });

  test('manual selections made offline are registered when the session arrives', () => {
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: FEEDS[1].label }));
    act(() => socket().open());
    send(session());
    ready(screen.getByLabelText(`${FEEDS[1].label} video`));
    expect(screen.getByRole('button', { name: 'Auto paused' })).toBeInTheDocument();
    expect(socket().sent).toContainEqual(expect.objectContaining({ type: 'select', match_id: 'cam-2', session_id: 'replay-1' }));
  });

  test('seven registered source identities render as one focused match and six alternatives', () => {
    const { container } = render(<App />);
    join();
    const strip = screen.getByRole('region', { name: 'Other matches' });
    expect(within(strip).getAllByRole('button')).toHaveLength(6);
    expect(screen.getByText('Monitoring 7 matches')).toBeInTheDocument();
    const previewNodes = Array.from(strip.querySelectorAll('video'));
    expect(new Set(previewNodes.map((video) => video.src)).size).toBe(7);
    const player = screen.getByLabelText(`${FEEDS[0].label} video`);
    ready(player);
    player.currentTime = 141.5;
    fireEvent.timeUpdate(player);
    fireEvent.click(screen.getByRole('button', { name: FEEDS[6].label }));
    const destination = nativePlayer(7);
    ready(destination);
    expect(screen.getByLabelText(`${FEEDS[6].label} video`)).toBe(destination);
    expect(destination).not.toBe(player);
    expect(destination.currentTime).toBe(141.5);
    expect(Array.from(strip.querySelectorAll('video'))).toEqual(previewNodes);
    expect(container.querySelectorAll('.tile--main video')).toHaveLength(1);
  });

  test('mode changes reach the backend and persist only after authoritative acknowledgement', () => {
    render(<App />);
    join();
    const mode = screen.getByRole('combobox', { name: 'Director mode' });
    expect(mode).toHaveValue('balanced');
    fireEvent.change(mode, { target: { value: 'all_action' } });
    expect(socket().sent).toContainEqual({ type: 'director_mode', session_id: 'replay-1', mode: 'all_action' });
    expect(screen.getByText('Applying All Action…')).toBeInTheDocument();
    expect(localStorage.getItem('matchpulse-director-mode')).toBeNull();
    expect(within(screen.getByLabelText('Applied director settings')).getByText('12s between switches')).toBeInTheDocument();
    send(session({ sequence: 2, director: director({ mode: 'all_action', config: {
      cooldown_seconds: 4, staying_bonus: .05, confirmations_required: 1,
    } }) }));
    expect(mode).toHaveValue('all_action');
    expect(localStorage.getItem('matchpulse-director-mode')).toBe('all_action');
    expect(screen.queryByText('Applying All Action…')).not.toBeInTheDocument();
    expect(within(screen.getByLabelText('Applied director settings')).getByText('4s between switches')).toBeInTheDocument();
  });

  test('persisted mode is requested on connection and rejected settings do not become active', () => {
    localStorage.setItem('matchpulse-director-mode', 'relaxed');
    render(<App />);
    join();
    expect(socket().sent).toContainEqual({ type: 'director_mode', session_id: 'replay-1', mode: 'relaxed' });
    send({ type: 'error', code: 'invalid_director_mode' });
    expect(screen.getByRole('combobox', { name: 'Director mode' })).toHaveValue('balanced');
    expect(screen.getByText(/could not be applied/)).toBeInTheDocument();
    expect(localStorage.getItem('matchpulse-director-mode')).toBe('relaxed');
    expect(within(screen.getByLabelText('Applied director settings')).getByText('12s between switches')).toBeInTheDocument();
  });

  test('mode changes do not reload media or change the replay clock', () => {
    render(<App />);
    join();
    const video = screen.getByLabelText(`${FEEDS[0].label} video`);
    video.currentTime = 151;
    fireEvent.timeUpdate(video);
    const source = video.src;
    fireEvent.change(screen.getByRole('combobox', { name: 'Director mode' }), { target: { value: 'relaxed' } });
    send(session({ sequence: 3, director: director({ mode: 'relaxed', config: {
      cooldown_seconds: 20, staying_bonus: .25, confirmations_required: 3,
    } }) }));
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBe(video);
    expect(video.src).toBe(source);
    expect(video.currentTime).toBe(151);
    expect(socket().sent.at(-1)).toMatchObject({ type: 'playback', position: 151 });
  });

  test('Auto off retains the focused source while genuine score updates continue', () => {
    render(<App />);
    join();
    fireEvent.click(screen.getByRole('button', { name: 'Auto' }));
    const matches = session().matches.map((match) => ({ ...match, healthy: match.match_id !== 'cam-1' }));
    send(session({ sequence: 3, auto_enabled: false, current_match_id: 'cam-1', matches,
      director: director({ latest_scores: { 'cam-7': .9 }, decision_reason: 'auto_disabled' }) }));
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBeInTheDocument();
    expect(within(screen.getByRole('button', { name: FEEDS[6].label })).getByText('Mock 0.900')).toBeInTheDocument();
    expect(screen.getByText('Monitoring 6 of 7 matches')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Auto off' })).toHaveAttribute('aria-pressed', 'false');
  });

  test('Watch keeps essential replay disclosure while history and About pages are removed', () => {
    const { container } = render(<App />);
    join({ director: director({ history: [{ id: 1, from_match_id: 'cam-1', to_match_id: 'cam-3',
      selected_at: Date.now() / 1000 - 18, playback_time: 110, kind: 'automatic', score: .732 }] }) });
    expect(screen.getByText(/Replay footage/)).toBeVisible();
    expect(screen.queryByText(/^Prerecorded replay$/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Watching$/)).not.toBeInTheDocument();
    expect(within(screen.getByRole('navigation', { name: 'Main navigation' })).getAllByRole('link')
      .map((link) => link.textContent)).toEqual(['Watch', 'Tables', 'News']);
    expect(container.querySelector('#moments')).toBeNull();
    expect(container.querySelector('#about')).toBeNull();
    expect(screen.queryByText(/Relative score 0.732/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Goal imminent|Attack building/)).not.toBeInTheDocument();
    expect(document.getElementById('watch')).toBeVisible();
    expect(container.querySelectorAll('video')).toHaveLength(7);
  });

  test('stale or offline scores are not presented as current monitoring activity', () => {
    render(<App />);
    join({ director: director({ scores_valid_until: Date.now() / 1000 - 1 }) });
    expect(screen.queryByText('Mock 0.800')).not.toBeInTheDocument();
    send(session({ sequence: 2, director: director() }));
    expect(screen.getByText('Mock 0.800')).toBeInTheDocument();
    act(() => socket().drop());
    expect(screen.queryByText('Mock 0.800')).not.toBeInTheDocument();
    expect(screen.queryByText('Monitoring 7 matches')).not.toBeInTheDocument();
    expect(screen.getByText(/Director offline/)).toBeInTheDocument();
  });

  test('a mode request does not revive a discarded cold-target recommendation after readiness', () => {
    render(<App />);
    join(undefined, false);
    send(recommend());
    fireEvent.change(screen.getByRole('combobox', { name: 'Director mode' }), { target: { value: 'relaxed' } });
    ready(nativePlayer(3));
    send(recommend());
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBeInTheDocument();
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
  });

  test('an authoritative mode change cannot recreate an old discarded automatic target', () => {
    render(<App />);
    join(undefined, false);
    send(recommend());
    send(session({ sequence: 3, director: director({ mode: 'relaxed', config: {
      cooldown_seconds: 20, staying_bonus: .25, confirmations_required: 3,
    } }) }));
    ready(nativePlayer(3));
    send(recommend());
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Director mode' })).toHaveValue('relaxed');
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
  });

  test('without a stored preference the browser respects the server initial mode', () => {
    render(<App />);
    join({ director: director({ mode: 'all_action', config: { cooldown_seconds: 4, staying_bonus: .05, confirmations_required: 1 } }) });
    expect(screen.getByRole('combobox', { name: 'Director mode' })).toHaveValue('all_action');
    expect(socket().sent.filter((message) => message.type === 'director_mode')).toHaveLength(0);
  });

  test('a discarded source becoming ready after recommendation expiry never sends a stale ACK', () => {
    jest.useFakeTimers();
    try {
      render(<App />);
      join(undefined, false);
      send(recommend());
      const video = nativePlayer(3);
      expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
      act(() => jest.advanceTimersByTime(3100));
      ready(video);
      expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBeInTheDocument();
      expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
      expect(screen.queryByText(`Switched to ${FEEDS[2].label}`)).not.toBeInTheDocument();
    } finally { jest.useRealTimers(); }
  });

  test('repeated cold-source proposals are not queued and a new ready proposal alone is acknowledged', () => {
    render(<App />);
    join(undefined, false);
    send(recommend());
    const video = nativePlayer(3);
    send(recommend({ sequence: 3 }));
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
    ready(video);
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBeInTheDocument();
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
    send(recommend({ sequence: 4 }));
    expect(screen.getByLabelText(`${FEEDS[2].label} video`)).toBe(video);
    expect(socket().sent.filter((message) => message.type === 'ack').map((message) => message.sequence)).toEqual([4]);
    expect(screen.getByText(`Switched to ${FEEDS[2].label}`)).toBeInTheDocument();
  });

  test.each(['automatic', 'auto_off', 'manual_hold'])('a hard player failure respects %s policy before an explicit fresh healthy fallback', (policy) => {
    render(<App />);
    join();
    if (policy === 'auto_off') fireEvent.click(screen.getByRole('button', { name: 'Auto' }));
    if (policy === 'manual_hold') {
      fireEvent.click(screen.getByRole('button', { name: FEEDS[1].label }));
      ready(screen.getByLabelText(`${FEEDS[1].label} video`));
    }
    const originalLabel = policy === 'manual_hold' ? `${FEEDS[1].label} video` : `${FEEDS[0].label} video`;
    const video = screen.getByLabelText(originalLabel);
    fireEvent.error(video);
    send(recommend({ selection_kind: 'fallback', selection_reason: 'unavailable_fallback' }));
    if (policy !== 'automatic') {
      expect(screen.getByLabelText(originalLabel)).toBe(video);
      expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
      return;
    }
    const destination = nativePlayer(3);
    expect(screen.getByLabelText(`${FEEDS[2].label} video`)).toBe(destination);
    expect(destination).not.toBe(video);
    expect(destination.currentTime).toBe(100);
    expect(socket().sent.filter((message) => message.type === 'ack').map((message) => message.sequence)).toEqual([2]);
  });

  test('ordinary buffering never turns an unavailable-fallback proposal into an automatic jump', () => {
    render(<App />);
    join();
    const video = screen.getByLabelText(`${FEEDS[0].label} video`);
    fireEvent.waiting(video);
    send(recommend({ selection_kind: 'fallback', selection_reason: 'unavailable_fallback' }));
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBe(video);
    expect(socket().sent.filter((message) => message.type === 'ack')).toHaveLength(0);
  });

  test('a pending mode request is cancelled honestly on timeout without persisting an unconfirmed setting', () => {
    jest.useFakeTimers();
    try {
      render(<App />);
      join();
      fireEvent.change(screen.getByRole('combobox', { name: 'Director mode' }), { target: { value: 'relaxed' } });
      act(() => jest.advanceTimersByTime(8001));
      expect(screen.getByRole('combobox', { name: 'Director mode' })).toHaveValue('balanced');
      expect(screen.getByText(/mode change was not confirmed/)).toBeInTheDocument();
      expect(localStorage.getItem('matchpulse-director-mode')).toBeNull();
    } finally { jest.useRealTimers(); }
  });

  test('a new session clears the previous scores without exposing removed history pages', () => {
    render(<App />);
    join({ director: director({ history: [{ id: 1, from_match_id: 'cam-1', to_match_id: 'cam-3',
      selected_at: Date.now() / 1000, playback_time: 100, kind: 'manual', score: .4 }] }) });
    const video = screen.getByLabelText(`${FEEDS[0].label} video`);
    expect(screen.getByText('Mock 0.800')).toBeInTheDocument();
    send(session({ session_id: 'replay-2', sequence: 1, director: director({ latest_scores: {}, scores_fresh: false }) }));
    expect(screen.queryByText('Mock 0.800')).not.toBeInTheDocument();
    expect(screen.getByLabelText(`${FEEDS[0].label} video`)).toBe(video);
    expect(screen.queryByRole('link', { name: 'Moments' })).not.toBeInTheDocument();
  });

  test('reconnects with backoff after the backend drops', () => {
    jest.useFakeTimers();
    try {
      render(<App />);
      act(() => socket().open());
      act(() => socket().drop());
      expect(within(screen.getByRole('banner')).queryByText('Offline')).not.toBeInTheDocument();
      expect(within(screen.getByRole('complementary', { name: 'Auto Director settings' }))
        .getByText(/Director offline/)).toBeInTheDocument();
      const before = global.FakeWebSocket.instances.length;
      act(() => { jest.advanceTimersByTime(1000); });
      expect(global.FakeWebSocket.instances.length).toBe(before + 1);
    } finally {
      jest.useRealTimers();
    }
  });
});
