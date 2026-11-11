import { act, fireEvent, render, screen } from '@testing-library/react';
import PersistentMatchPlayer from './PersistentMatchPlayer';
import useSourcePlayback from './useSourcePlayback';
import { getVideoUrl, playbackIdentity } from '../sources/feeds';
import { URL_RENEWAL_LEAD_MS } from './useMediaAuthorization';

function CaptionClock({ media, feed }) {
  const time = useSourcePlayback(media, `${getVideoUrl(feed)}\n${feed.cameraId}`, true, playbackIdentity(feed));
  return <output data-testid="authorized-clock">{time ?? 'pending'}</output>;
}

let play;
beforeEach(() => {
  jest.useFakeTimers();
  play = jest.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(function start() {
    Object.defineProperty(this, 'paused', { configurable: true, value: false });
    return Promise.resolve();
  });
  jest.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(function stop() {
    Object.defineProperty(this, 'paused', { configurable: true, value: true });
  });
  jest.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
});
afterEach(() => { jest.restoreAllMocks(); jest.useRealTimers(); });

function authorizedPlayer(playing = true) {
  const initial = { id: 1, cameraId: 'cam-1', label: 'Private match', sourceKey: 'match-1.mp4',
    sourceIdentity: 'a'.repeat(64), offsetSeconds: 120,
    url: 'https://private.example/match-1.mp4?signature=first', urlExpiresAt: Date.now() / 1000 + 120 };
  let feed = initial;
  const clock = { current: { time: 250, playing, rate: 1.25, muted: false, volume: .4,
    stalled: false, available: true, ended: false } };
  const media = { current: null };
  const focus = { current: 1 };
  const reports = [];
  const report = () => reports.push({ ...clock.current });
  const tree = () => <><PersistentMatchPlayer feed={feed} focused focusedRef={focus} clock={clock}
    mediaRef={media} audioAllowed onPlayback={report} />
    <CaptionClock media={media} feed={feed} /></>;
  const view = render(tree());
  const video = view.container.querySelector('video');
  const ready = () => {
    Object.defineProperties(video, { duration: { configurable: true, value: 3600 },
      readyState: { configurable: true, value: 1 }, seeking: { configurable: true, value: false } });
    fireEvent.loadedMetadata(video);
    Object.defineProperty(video, 'readyState', { configurable: true, value: 2 });
    fireEvent.loadedData(video);
  };
  ready();
  video.matchpulseAudioOwned = true; video.muted = false;
  return { ...view, video, clock, media, reports, ready, initial,
    update(next) { feed = { ...feed, ...next }; view.rerender(tree()); },
  };
}

test.each([true, false])('expiry renewal preserves same-element source position and playing=%s without clock reset', (playing) => {
  const fixture = authorizedPlayer(playing);
  const { video, clock } = fixture;
  const originalSrc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src').set;
  const originalTime = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'currentTime').set;
  const writes = jest.spyOn(HTMLMediaElement.prototype, 'src', 'set').mockImplementation(function authorize(value) {
    originalSrc.call(this, value);
    // Model the native reset on src replacement; restoring this position must not
    // be mistaken for a user seek or leak zero into the replay playback reports.
    originalTime.call(this, 0);
    Object.defineProperties(this, { readyState: { configurable: true, value: 0 },
      paused: { configurable: true, value: true } });
  });
  const seeks = jest.spyOn(HTMLMediaElement.prototype, 'currentTime', 'set');
  play.mockClear(); fixture.reports.length = 0;
  fixture.update({ url: 'https://private.example/match-1.mp4?signature=second', urlExpiresAt: Date.now() / 1000 + 3600 });
  expect(video.getAttribute('src')).toBe(fixture.initial.url);
  expect(screen.getByTestId('authorized-clock')).toHaveTextContent('370');
  expect(writes).not.toHaveBeenCalled(); expect(seeks).not.toHaveBeenCalled();
  act(() => jest.advanceTimersByTime(120000 - URL_RENEWAL_LEAD_MS - 1));
  expect(writes).not.toHaveBeenCalled();
  act(() => jest.advanceTimersByTime(1));
  expect(fixture.container.querySelector('video')).toBe(video);
  expect(writes).toHaveBeenCalledTimes(1);
  expect(video.getAttribute('src')).toContain('signature=second');
  expect(clock.current.time).toBe(250); expect(clock.current.stalled).toBe(true);
  expect(clock.current.playing).toBe(playing); expect(clock.current.muted).toBe(false);
  expect(screen.getByTestId('authorized-clock')).toHaveTextContent('pending');
  fireEvent.seeking(video); // Native reset events during authorization reload are not user seeks.
  expect(clock.current.time).toBe(250);
  fixture.ready();
  expect(seeks.mock.calls.map(([value]) => value)).toEqual([370]);
  expect(clock.current.time).toBe(250); expect(clock.current.stalled).toBe(false);
  expect(clock.current.playing).toBe(playing); expect(video.paused).toBe(!playing);
  expect(video.playbackRate).toBe(1.25); expect(video.volume).toBe(.4); expect(video.muted).toBe(false);
  expect(video.matchpulseReadyIdentity).toBe(playbackIdentity(fixture.initial));
  expect(screen.getByTestId('authorized-clock')).toHaveTextContent('370');
  expect(fixture.reports.length).toBeGreaterThan(0);
  expect(fixture.reports.every((report) => report.time === 250 && report.playing === playing)).toBe(true);
  if (!playing) expect(play).not.toHaveBeenCalled();
});

test('an expired range request recovers with the newest authorization before its planned renewal', () => {
  const fixture = authorizedPlayer();
  fixture.update({ url: 'https://private.example/match-1.mp4?signature=recovery', urlExpiresAt: Date.now() / 1000 + 3600 });
  expect(fixture.video.getAttribute('src')).toBe(fixture.initial.url);
  fireEvent.error(fixture.video);
  expect(fixture.container.querySelector('video')).toBe(fixture.video);
  expect(fixture.video.getAttribute('src')).toContain('signature=recovery');
  expect(fixture.clock.current.time).toBe(250);
  fixture.ready();
  expect(fixture.clock.current.available).toBe(true); expect(fixture.clock.current.stalled).toBe(false);
  expect(fixture.video.matchpulseReadyIdentity).toBe(playbackIdentity(fixture.initial));
});

test('an error with no fresh URL waits for registration and cleanup cancels renewal timers', () => {
  const fixture = authorizedPlayer();
  fireEvent.error(fixture.video);
  expect(fixture.clock.current.available).toBe(false);
  fixture.update({ url: 'https://private.example/match-1.mp4?signature=later', urlExpiresAt: Date.now() / 1000 + 3600 });
  expect(fixture.video.getAttribute('src')).toContain('signature=later');
  fixture.ready();
  expect(fixture.clock.current.available).toBe(true);
  expect(fixture.video.muted).toBe(false);
  const writes = jest.spyOn(HTMLMediaElement.prototype, 'src', 'set');
  fixture.unmount();
  act(() => jest.advanceTimersByTime(4000000));
  expect(writes).not.toHaveBeenCalled();
});

test('a renewal landing 50ms behind is corrected before any ready clock or caption is published', () => {
  const fixture = authorizedPlayer();
  const { video, clock } = fixture;
  const nativeTime = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'currentTime').set;
  fixture.update({ url: 'https://private.example/match-1.mp4?signature=precise', urlExpiresAt: Date.now() / 1000 + 3600 });
  fireEvent.error(video); // Recover through the new authorization without waiting for expiry.
  nativeTime.call(video, 0);
  Object.defineProperties(video, { readyState: { configurable: true, value: 1 },
    seeking: { configurable: true, value: false } });
  const seeks = jest.spyOn(HTMLMediaElement.prototype, 'currentTime', 'set');
  fixture.reports.length = 0;
  fireEvent.loadedMetadata(video);
  expect(seeks.mock.calls.map(([value]) => value)).toEqual([370]);
  nativeTime.call(video, 369.95);
  Object.defineProperty(video, 'readyState', { configurable: true, value: 2 });
  fireEvent.loadedData(video);
  expect(seeks.mock.calls.map(([value]) => value)).toEqual([370, 370]);
  expect(clock.current.time).toBe(250); expect(clock.current.stalled).toBe(true);
  expect(video.matchpulseReadyIdentity).toBeNull();
  expect(screen.getByTestId('authorized-clock')).toHaveTextContent('pending');
  fireEvent.seeked(video);
  expect(video.currentTime).toBe(370);
  expect(clock.current.time).toBe(video.currentTime - fixture.initial.offsetSeconds);
  expect(clock.current.stalled).toBe(false);
  expect(screen.getByTestId('authorized-clock')).toHaveTextContent('370');
  expect(fixture.reports.every((report) => report.time === 250)).toBe(true);
});

test('a decoder that repeatedly misses renewal alignment becomes unavailable after bounded correction', () => {
  const fixture = authorizedPlayer();
  const { video, clock } = fixture;
  const nativeTime = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'currentTime').set;
  fixture.update({ url: 'https://private.example/match-1.mp4?signature=coarse', urlExpiresAt: Date.now() / 1000 + 3600 });
  fireEvent.error(video);
  const seeks = jest.spyOn(HTMLMediaElement.prototype, 'currentTime', 'set');
  nativeTime.call(video, 369.95);
  fixture.reports.length = 0;
  for (let attempt = 0; attempt < 8; attempt += 1) {
    nativeTime.call(video, 369.95);
    fireEvent.loadedData(video);
  }
  expect(seeks.mock.calls.map(([value]) => value)).toEqual([370, 370, 370]);
  expect(clock.current.time).toBe(250); expect(clock.current.available).toBe(false);
  expect(clock.current.stalled).toBe(true); expect(video.matchpulseReadyIdentity).toBeNull();
  expect(screen.getByText('Video unavailable.')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  expect(fixture.reports.every((report) => report.time === 250)).toBe(true);
});
