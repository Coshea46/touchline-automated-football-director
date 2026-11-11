import { act, fireEvent, render, screen } from '@testing-library/react';
import App from '../app/App';
import useAudioOwnership from '../features/watch/audio/useAudioOwnership';
import { FEEDS, getVideoUrl } from '../features/watch/sources/feeds';
import { LIVE_CAPTION_MODEL } from '../features/captions/liveCaptions';

const mockAudioRequest = jest.fn();
const mockAudioRelease = jest.fn();
jest.mock('../features/watch/audio/useAudioOwnership', () => jest.fn());

const socket = () => global.FakeWebSocket.instances.at(-1);
const receive = (value) => act(() => socket().receive(value));
const MODEL = 'temporal-shots3_10-d63fff64480d/classifier.pt';
const deadline = () => Date.now() / 1000 + 8 * 60 * 60;
const snapshot = (extra = {}) => ({ type: 'session', protocol_version: 1, session_id: 'shared-replay', sequence: 1,
  state: 'running', mode: 'real', model_version: MODEL, server_time: Date.now() / 1000,
  controls_enabled: false, can_claim_control: true, auto_requested: false, auto_enabled: false, auto_available: true,
  auto_can_start: false, auto_starting: false, current_match_id: 'cam-1', manual_hold_until: 0,
  playback: { position: 10, playing: true, rate: 1, clock_epoch: 2 },
  matches: FEEDS.map((feed) => ({ match_id: feed.cameraId, label: feed.label, url: getVideoUrl(feed),
    source_key: feed.sourceKey, source_identity: feed.sourceIdentity, offset_seconds: 120, healthy: true })),
  director: { mode: 'balanced', config: { cooldown_seconds: 12, staying_bonus: .2, confirmations_required: 2 },
    latest_scores: {}, scores_fresh: false, history: [] },
  captions: { available: true, configured: true, enabled: true, user_enabled: true, status: 'paused',
    model: LIVE_CAPTION_MODEL, generation: 1, clock_epoch: 2, match_id: 'cam-1', deadline: deadline() },
  ...extra });

function ready(video) {
  Object.defineProperties(video, { readyState: { configurable: true, value: 2 },
    duration: { configurable: true, value: 3600 }, seeking: { configurable: true, value: false } });
  fireEvent.loadedMetadata(video); fireEvent.loadedData(video);
}

function join(extra = {}) {
  const view = render(<App />);
  act(() => { socket().open(); socket().receive(snapshot(extra)); });
  const video = view.container.querySelector('.tile--main video');
  ready(video);
  return { ...view, video, ws: socket() };
}

beforeEach(() => {
  localStorage.clear(); window.history.replaceState(null, '', '/');
  mockAudioRequest.mockReset().mockResolvedValue(true); mockAudioRelease.mockReset();
  useAudioOwnership.mockImplementation(({ enabled }) => ({ owned: enabled, pending: false,
    status: enabled ? 'owned' : 'other-tab', requestAudio: mockAudioRequest, releaseAudio: mockAudioRelease }));
});
afterEach(() => jest.useRealTimers());

test('Use this tab claims explicitly and waits for role acknowledgement before reporting the actual focused clock or requesting audio', async () => {
  localStorage.setItem('matchpulse-director-mode', 'all_action');
  const { container, video: original, ws, rerender } = join();
  const videos = Array.from(container.querySelectorAll('video'));
  expect(videos).toHaveLength(7);
  expect(ws.sent).toHaveLength(0);
  rerender(<App />);
  expect(ws.sent).toHaveLength(0); // Re-render/observer connection never steals control.
  fireEvent.click(screen.getByRole('button', { name: FEEDS[1].label }));
  const selected = container.querySelector('.tile--main video');
  ready(selected);
  selected.currentTime = 183.5;
  fireEvent.timeUpdate(selected); fireEvent.pause(selected);
  const source = selected.getAttribute('src');
  const request = screen.getByRole('button', { name: 'Use this tab' });
  expect(request).toBeEnabled();
  fireEvent.click(request);
  fireEvent.click(request);
  expect(ws.sent).toEqual([{ type: 'claim_control', session_id: 'shared-replay' }]);
  expect(request).toBeDisabled();
  expect(request).toHaveAttribute('aria-busy', 'true');
  expect(screen.getByRole('button', { name: 'Auto off' })).toBeDisabled();
  expect(mockAudioRequest).not.toHaveBeenCalled();
  receive(snapshot({ sequence: 2 }));
  expect(screen.getByRole('button', { name: 'Use this tab' })).toBeDisabled();
  const originalDeadline = deadline();
  receive(snapshot({ sequence: 3, controls_enabled: true,
    captions: { ...snapshot().captions, generation: 2, deadline: originalDeadline } }));
  await act(async () => { await Promise.resolve(); });
  expect(screen.queryByRole('button', { name: 'Use this tab' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Auto off' })).toBeEnabled();
  expect(screen.getByRole('combobox', { name: 'Director mode' })).toHaveValue('balanced');
  expect(ws.sent.filter((message) => ['auto', 'director_mode', 'select', 'captions'].includes(message.type))).toHaveLength(0);
  expect(ws.sent.filter((message) => message.type === 'playback').at(-1)).toMatchObject({
    session_id: 'shared-replay', match_id: 'cam-2', position: 63.5, playing: false, available: true,
  });
  expect(mockAudioRequest).toHaveBeenCalledTimes(1);
  expect(useAudioOwnership.mock.calls.at(-1)[0].enabled).toBe(true);
  expect(container.querySelector('.tile--main video')).toBe(selected);
  expect(selected).not.toBe(original);
  expect(selected.currentTime).toBe(183.5);
  expect(selected.getAttribute('src')).toBe(source);
  Array.from(container.querySelectorAll('video')).forEach((element, index) => expect(element).toBe(videos[index]));
});

test('an older observer backend explains the required restart without offering an unsupported claim', () => {
  const { ws } = join({ can_claim_control: undefined });
  expect(screen.getByText(/Restart the backend to enable control handoff/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Use this tab' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Play audio here' })).toBeDisabled();
  expect(ws.sent).toHaveLength(0);
  expect(mockAudioRequest).not.toHaveBeenCalled();
});

test('rejected, timed-out and disconnected claims leave controls locked and never retry automatically', () => {
  jest.useFakeTimers();
  const { ws } = join();
  fireEvent.click(screen.getByRole('button', { name: 'Use this tab' }));
  receive({ type: 'error', session_id: 'shared-replay', sequence: 2, code: 'session_closed' });
  expect(screen.getByText(/This tab could not take control/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Use this tab' })).toBeEnabled();
  expect(screen.getByRole('button', { name: 'Auto off' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Use this tab' }));
  act(() => jest.advanceTimersByTime(8000));
  expect(screen.getByText('Control was not confirmed. Try again.')).toBeInTheDocument();
  expect(ws.sent.filter((message) => message.type === 'claim_control')).toHaveLength(2);
  fireEvent.click(screen.getByRole('button', { name: 'Use this tab' }));
  act(() => ws.drop());
  expect(screen.getByText(/The replay connection was lost/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Use this tab' })).not.toBeInTheDocument();
  act(() => jest.advanceTimersByTime(1000));
  const reconnected = socket();
  act(() => { reconnected.open(); reconnected.receive(snapshot({ sequence: 10 })); });
  expect(screen.getByRole('button', { name: 'Use this tab' })).toBeEnabled();
  expect(reconnected.sent.filter((message) => message.type === 'claim_control')).toHaveLength(0);
  expect(mockAudioRequest).not.toHaveBeenCalled();
});

test('losing the backend role stops writes and releases audio without moving or pausing the native player', () => {
  const { ws, video, container } = join({ controls_enabled: true });
  const nodes = Array.from(container.querySelectorAll('video'));
  const position = video.currentTime;
  const pause = jest.spyOn(video, 'pause'); pause.mockClear();
  ws.sent = []; mockAudioRelease.mockClear();
  receive(snapshot({ sequence: 2, controls_enabled: false }));
  expect(mockAudioRelease).toHaveBeenCalled();
  expect(useAudioOwnership.mock.calls.at(-1)[0].enabled).toBe(false);
  expect(screen.getByRole('button', { name: 'Auto off' })).toBeDisabled();
  fireEvent.timeUpdate(video);
  expect(ws.sent).toHaveLength(0);
  expect(video.currentTime).toBe(position);
  expect(pause).not.toHaveBeenCalled();
  Array.from(container.querySelectorAll('video')).forEach((element, index) => expect(element).toBe(nodes[index]));
  pause.mockRestore();
});

test('a prepared waiting backend does not freeze playback before Auto is requested', () => {
  const { video } = join({ mode: 'disabled', state: 'waiting', model_version: 'disabled',
    controls_enabled: true, auto_available: false, auto_can_start: true, auto_starting: false, auto_start_reason: 'available' });
  expect(screen.queryByText('Starting inference...')).not.toBeInTheDocument();
  expect(screen.getByText('Ready to start monitoring')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Auto off' })).toBeEnabled();
  expect(video.currentTime).toBe(130);
});
