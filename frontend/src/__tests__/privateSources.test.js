import { act, fireEvent, render } from '@testing-library/react';
import App from '../app/App';
import { FEEDS, getVideoUrl } from '../features/watch/sources/feeds';

jest.mock('../features/watch/sources/feeds', () => {
  const previous = process.env.REACT_APP_SOURCE_ACCESS;
  delete process.env.REACT_APP_SOURCE_ACCESS;
  try { return jest.requireActual('../features/watch/sources/feeds'); }
  finally { if (previous !== undefined) process.env.REACT_APP_SOURCE_ACCESS = previous; }
});

const session = (sequence, signature) => ({ type: 'session', protocol_version: 1,
  session_id: 'private-source-session', model_version: 'private-model', mode: 'real', state: 'running',
  sequence, server_time: Date.now() / 1000, current_match_id: 'cam-1', auto_available: true, auto_enabled: true,
  playback: { position: 10, clock_epoch: 7 }, matches: FEEDS.map((feed, index) => ({
    match_id: feed.cameraId, label: feed.label, source_key: feed.sourceKey,
    source_identity: String(index + 1).repeat(64), offset_seconds: 120, healthy: true,
    url: `https://private.example/${feed.sourceKey}?signature=${signature}`,
    url_expires_at: Date.now() / 1000 + 3600,
  })) });

beforeEach(() => { localStorage.clear(); window.history.replaceState(null, '', '/'); });

test('the private-default app mounts no anonymous sources before signed backend registration', () => {
  const play = jest.spyOn(HTMLMediaElement.prototype, 'play');
  const load = jest.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  const { container } = render(<App />);
  try {
    const videos = [...container.querySelectorAll('video')];
    expect(videos).toHaveLength(7);
    videos.forEach((video) => expect(video).not.toHaveAttribute('src'));
    FEEDS.forEach((feed) => expect(getVideoUrl(feed)).toBe(''));
    expect(play).not.toHaveBeenCalled(); expect(load).not.toHaveBeenCalled();
  } finally { jest.restoreAllMocks(); }
});

test('rotated signatures preserve warm players and a fresh automatic ACK in the same clock epoch', () => {
  const load = jest.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  const { container } = render(<App />);
  const socket = global.FakeWebSocket.instances.at(-1);
  act(() => { socket.open(); socket.receive(session(1, 'first')); });
  const videos = [...container.querySelectorAll('video')];
  videos.forEach((video) => {
    Object.defineProperties(video, { duration: { configurable: true, value: 3600 },
      readyState: { configurable: true, value: 2 }, seeking: { configurable: true, value: false } });
    fireEvent.loadedMetadata(video); fireEvent.loadedData(video);
  });
  // The initial join correctly reports the pre-decode clock while seeking to 10.
  // Only rotation and the ensuing promotion are under test below.
  socket.sent.length = 0;
  const writes = jest.spyOn(HTMLMediaElement.prototype, 'src', 'set');
  const seeks = jest.spyOn(HTMLMediaElement.prototype, 'currentTime', 'set');
  load.mockClear();
  try {
    act(() => socket.receive(session(2, 'second')));
    videos.forEach((video, index) => {
      expect(container.querySelectorAll('video')[index]).toBe(video);
      expect(video.getAttribute('src')).toContain('signature=first');
    });
    act(() => socket.receive({ type: 'recommendation', protocol_version: 1,
      session_id: 'private-source-session', model_version: 'private-model', sequence: 3, clock_epoch: 7,
      score_kind: 'model', selected_match_id: 'cam-2', playback_time: 10, observation_end: 10,
      emitted_at: Date.now() / 1000, valid_until: Date.now() / 1000 + 3,
      scores: Object.fromEntries(FEEDS.map((feed) => [feed.cameraId, .5])) }));
    expect(container.querySelector('.tile--main video')).toBe(videos[1]);
    expect(socket.sent.find((message) => message.type === 'ack')).toMatchObject({ sequence: 3, clock_epoch: 7, match_id: 'cam-2' });
    expect(writes).not.toHaveBeenCalled(); expect(seeks).not.toHaveBeenCalled(); expect(load).not.toHaveBeenCalled();
    expect(socket.sent.filter((message) => message.type === 'playback').every((message) => message.position === 10)).toBe(true);
  } finally { jest.restoreAllMocks(); }
});

test('proactive authorization renewal never fabricates a switch ACK or publishes a reset playback position', () => {
  jest.useFakeTimers();
  const load = jest.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  const { container } = render(<App />);
  try {
    const socket = global.FakeWebSocket.instances.at(-1);
    const initial = session(1, 'initial');
    initial.matches.forEach((match) => { match.url_expires_at = Date.now() / 1000 + 120; });
    act(() => { socket.open(); socket.receive(initial); });
    const videos = [...container.querySelectorAll('video')];
    const ready = (video) => {
      Object.defineProperties(video, { duration: { configurable: true, value: 3600 },
        readyState: { configurable: true, value: 2 }, seeking: { configurable: true, value: false } });
      fireEvent.loadedMetadata(video); fireEvent.loadedData(video);
    };
    videos.forEach(ready);
    act(() => socket.receive(session(2, 'renewed')));
    socket.sent.length = 0; load.mockClear();
    act(() => jest.advanceTimersByTime(90000));
    videos.forEach((video, index) => {
      expect(container.querySelectorAll('video')[index]).toBe(video);
      expect(video.getAttribute('src')).toContain('signature=renewed');
    });
    videos.forEach(ready);
    const reports = socket.sent.filter((message) => message.type === 'playback');
    expect(reports.length).toBeGreaterThan(0);
    expect(reports.every((message) => message.position === 10)).toBe(true);
    expect(socket.sent.filter((message) => message.type === 'ack')).toEqual([]);
  } finally { jest.restoreAllMocks(); jest.useRealTimers(); }
});
