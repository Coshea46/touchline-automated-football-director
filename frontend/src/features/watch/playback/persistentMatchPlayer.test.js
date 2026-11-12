import { fireEvent, render, screen } from '@testing-library/react';
import { useLayoutEffect } from 'react';
import PersistentMatchPlayer from './PersistentMatchPlayer';
import { getVideoUrl } from '../sources/feeds';
import useSourcePlayback from './useSourcePlayback';

const FEEDS = Array.from({ length: 7 }, (_, index) => ({
  id: index + 1, cameraId: `cam-${index + 1}`, label: `Test Match ${index + 1}`,
  url: `https://media.example/full-${index + 1}.mp4`,
  previewUrl: `https://media.example/preview-${index + 1}.mp4`,
  offsetSeconds: index === 0 ? 120 : index === 1 ? 60 : 0,
}));

let play;
let pause;
let load;
beforeEach(() => {
  jest.useFakeTimers();
  // jsdom's default no-op play/pause mocks leave paused=true forever. Model
  // native state locally so an unnecessary promotion restart cannot hide.
  play = jest.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(function start() {
    Object.defineProperty(this, 'paused', { configurable: true, value: false });
    return Promise.resolve();
  });
  pause = jest.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(function stop() {
    Object.defineProperty(this, 'paused', { configurable: true, value: true });
  });
  load = jest.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
});
afterEach(() => { jest.restoreAllMocks(); jest.useRealTimers(); });

function CaptionClock({ media, feed }) {
  const source = `${getVideoUrl(feed)}\n${feed.cameraId}`;
  const time = useSourcePlayback(media, source, true);
  return <output data-testid="caption-clock">{feed.cameraId}:{time ?? 'pending'}</output>;
}

function AudioLease({ media, clock, allowed, sourceId }) {
  // The separate ownership hook is tested with real lock scheduling elsewhere.
  // Model its synchronous grant/revocation callback for this component contract.
  useLayoutEffect(() => {
    const video = media.current;
    if (video) { video.matchpulseAudioOwned = allowed; video.muted = !allowed || clock.current.muted; }
  }, [media, clock, allowed, sourceId]);
  return null;
}

function pool({ playing = true, caption = false, ...options } = {}) {
  const clock = { current: { time: 250, playing, rate: 1.25, muted: false, volume: .4,
    stalled: false, available: true, ended: false } };
  const media = { current: null };
  const focusedRef = { current: 1 };
  const onPlayback = jest.fn();
  const onAvailability = jest.fn();
  let active = 1;
  let settings = { audioAllowed: true, ...options };
  const tree = () => <div data-testid="persistent-pool">
    {FEEDS.map((feed) => <PersistentMatchPlayer key={feed.cameraId} feed={feed} focused={feed.id === active}
      focusedRef={focusedRef} clock={clock} mediaRef={media} onPlayback={onPlayback}
      onAvailability={onAvailability} onClick={() => {}} {...settings} />)}
    <AudioLease media={media} clock={clock} allowed={settings.audioAllowed} sourceId={active} />
    {caption && <CaptionClock media={media} feed={FEEDS[active - 1]} />}
  </div>;
  const view = render(tree());
  const video = (id) => view.container.querySelector(`[data-feed-id="${id}"] video`);
  const ready = (id) => {
    const element = video(id);
    Object.defineProperties(element, {
      duration: { configurable: true, value: 3600 },
      readyState: { configurable: true, value: 1 },
      seeking: { configurable: true, value: false },
    });
    fireEvent.loadedMetadata(element);
    Object.defineProperty(element, 'readyState', { configurable: true, value: 2 });
    fireEvent.loadedData(element);
  };
  return { ...view, clock, media, focusedRef, onPlayback, onAvailability, video, ready,
    readyAll: () => FEEDS.forEach((feed) => ready(feed.id)),
    focus(id) {
      // Root App owns this synchronous silence-before-focus step.
      if (media.current && active !== id) {
        media.current.matchpulseAudioOwned = false;
        media.current.muted = true;
        delete clock.current.sample;
        clock.current.stalled = true;
      }
      focusedRef.current = id;
      active = id;
      view.rerender(tree());
    },
    update(next) { settings = { ...settings, ...next }; view.rerender(tree()); },
  };
}

test('seven persistent nodes use actual source URLs, with only the focused player exposing controls', () => {
  const fixture = pool();
  expect(fixture.container.querySelectorAll('video')).toHaveLength(7);
  for (const feed of FEEDS) {
    expect(fixture.video(feed.id)).toHaveAttribute('src', feed.url);
    expect(fixture.video(feed.id)).not.toHaveAttribute('src', feed.previewUrl);
  }
  expect(screen.getByLabelText('Test Match 1 video')).toBe(fixture.video(1));
  expect(fixture.container.querySelectorAll('video[controls]')).toHaveLength(1);
  expect(screen.getAllByRole('button', { name: /Test Match/ })).toHaveLength(6);
});

test('ready aligned promotions reuse nodes without source writes, loads, seeks or playback restarts', () => {
  const fixture = pool();
  fixture.readyAll();
  const nodes = FEEDS.map((feed) => fixture.video(feed.id));
  const parents = nodes.map((video) => video.parentElement);
  const sourceWrites = jest.spyOn(HTMLMediaElement.prototype, 'src', 'set');
  const seekWrites = jest.spyOn(HTMLMediaElement.prototype, 'currentTime', 'set');
  const attributeWrites = nodes.map((video) => jest.spyOn(video, 'setAttribute'));
  play.mockClear(); pause.mockClear(); load.mockClear();
  fixture.focus(2);
  fixture.focus(7);
  fixture.focus(1);
  for (const [index, feed] of FEEDS.entries()) {
    expect(fixture.video(feed.id)).toBe(nodes[index]);
    expect(nodes[index].parentElement).toBe(parents[index]);
    expect(nodes[index]).toHaveAttribute('src', feed.url);
    expect(attributeWrites[index].mock.calls.filter(([name]) => name === 'src')).toHaveLength(0);
  }
  expect(fixture.media.current).toBe(nodes[0]);
  expect(fixture.clock.current.time).toBe(250);
  expect(sourceWrites).not.toHaveBeenCalled();
  expect(seekWrites).not.toHaveBeenCalled();
  expect(load).not.toHaveBeenCalled();
  expect(play).not.toHaveBeenCalled();
  expect(pause).not.toHaveBeenCalled();
});

test('handoff mutes the outgoing source before unmuting the new source, and lease loss preserves mute intent', () => {
  const fixture = pool();
  fixture.readyAll();
  const nodes = FEEDS.map((feed) => fixture.video(feed.id));
  const muted = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'muted');
  const audibleCounts = [];
  jest.spyOn(HTMLMediaElement.prototype, 'muted', 'set').mockImplementation(function setMuted(value) {
    muted.set.call(this, value);
    audibleCounts.push(nodes.filter((node) => !node.muted && !node.paused).length);
  });
  fixture.focus(2);
  expect(audibleCounts.every((count) => count <= 1)).toBe(true);
  expect({ desiredMuted: fixture.clock.current.muted, targetMuted: fixture.video(2).muted,
    targetPaused: fixture.video(2).paused, owned: fixture.video(2).matchpulseAudioOwned }).toEqual({
    desiredMuted: false, targetMuted: false, targetPaused: false, owned: true,
  });
  expect(nodes.filter((node) => !node.muted && !node.paused)).toEqual([fixture.video(2)]);
  expect(fixture.video(1).muted).toBe(true);
  play.mockClear(); pause.mockClear();
  fixture.update({ audioAllowed: false });
  expect(nodes.every((node) => node.muted)).toBe(true);
  expect(fixture.clock.current.muted).toBe(false);
  fixture.update({ audioAllowed: true });
  expect(nodes.filter((node) => !node.muted && !node.paused)).toEqual([fixture.video(2)]);
  expect(play).not.toHaveBeenCalled();
  expect(pause).not.toHaveBeenCalled();
});

test('source offsets are preserved and a warm target reports its actual slightly-behind clock without seeking', () => {
  const fixture = pool();
  fixture.readyAll();
  expect(fixture.video(1).currentTime).toBe(370);
  expect(fixture.video(2).currentTime).toBe(310);
  fixture.video(2).currentTime = 309.95;
  const seekWrites = jest.spyOn(HTMLMediaElement.prototype, 'currentTime', 'set');
  fixture.focus(2);
  expect(fixture.clock.current.time).toBeCloseTo(249.95, 6);
  expect(fixture.video(2).currentTime).toBe(309.95);
  expect(seekWrites).not.toHaveBeenCalled();
  expect(fixture.media.current).toBe(fixture.video(2));
});

test('captions observe the new actual DOM clock immediately instead of retaining the outgoing media', () => {
  const fixture = pool({ caption: true });
  fixture.readyAll();
  expect(screen.getByTestId('caption-clock')).toHaveTextContent('cam-1:370');
  fixture.focus(2);
  expect(fixture.media.current).toBe(fixture.video(2));
  expect(fixture.video(2).matchpulseReadySource).toBe(`${FEEDS[1].url}\ncam-2`);
  expect(screen.getByTestId('caption-clock')).toHaveTextContent('cam-2:310');
  fixture.video(1).currentTime = 999;
  fireEvent.timeUpdate(fixture.video(1));
  expect(screen.getByTestId('caption-clock')).toHaveTextContent('cam-2:310');
  expect(fixture.clock.current.time).toBe(250);
});

test('a paused pool stays paused across promotion and an explicit seek aligns every source once', () => {
  const fixture = pool({ playing: false });
  fixture.readyAll();
  play.mockClear();
  fixture.focus(2);
  expect(FEEDS.every((feed) => fixture.video(feed.id).paused)).toBe(true);
  expect(fixture.clock.current.playing).toBe(false);
  expect(play).not.toHaveBeenCalled();
  fixture.update({ seekTo: { time: 400 } });
  for (const feed of FEEDS) {
    expect(fixture.video(feed.id).currentTime).toBe(400 + feed.offsetSeconds);
    expect(fixture.video(feed.id).playbackRate).toBe(1.25);
  }
  expect(fixture.clock.current.time).toBe(250); // A requested seek is not decoded permission.
  FEEDS.forEach((feed) => fireEvent.loadedData(fixture.video(feed.id)));
  expect(fixture.clock.current.time).toBe(400);
  expect(play).not.toHaveBeenCalled();
});

test('an unready selected target cannot signal readiness until decoded and finished seeking', () => {
  const fixture = pool();
  fixture.readyAll();
  const target = fixture.video(2);
  target.currentTime = 310;
  Object.defineProperties(target, {
    readyState: { configurable: true, value: 1 }, seeking: { configurable: true, value: true },
  });
  fireEvent.seeking(target);
  fixture.onAvailability.mockClear();
  fixture.focus(2);
  expect(fixture.clock.current.time).toBe(250);
  expect(fixture.clock.current.stalled).toBe(true);
  expect(target.matchpulseReadySource).toBeNull();
  Object.defineProperty(target, 'readyState', { configurable: true, value: 2 });
  fireEvent.loadedData(target);
  expect(fixture.onAvailability.mock.calls.filter(([id, ready]) => id === 'cam-2' && ready)).toHaveLength(0);
  Object.defineProperty(target, 'seeking', { configurable: true, value: false });
  fireEvent.loadedData(target);
  expect(fixture.clock.current.stalled).toBe(false);
  expect(fixture.onAvailability).toHaveBeenLastCalledWith('cam-2', true);
  expect(target.matchpulseReadySource).toBe(`${FEEDS[1].url}\ncam-2`);
  expect(fixture.clock.current.time).toBe(250);
});

test('background media events cannot change the focused playback state or recover its failure', () => {
  const fixture = pool();
  fixture.readyAll();
  fixture.focus(2);
  const background = fixture.video(1);
  background.currentTime = 900;
  fireEvent.pause(background);
  fireEvent.waiting(background);
  fireEvent.timeUpdate(background);
  expect(fixture.clock.current.playing).toBe(true);
  expect(fixture.clock.current.time).toBe(250);
  expect(fixture.clock.current.stalled).toBe(false);
  fireEvent.error(fixture.video(2));
  expect(fixture.clock.current.available).toBe(false);
  expect(fixture.clock.current.stalled).toBe(true);
  expect(fixture.clock.current.time).toBe(250);
  fireEvent.loadedData(background);
  expect(fixture.clock.current.available).toBe(false);
  expect(fixture.clock.current.time).toBe(250);
});

test('a network stalled event with decoded buffered playback does not freeze the promoted native clock', () => {
  const fixture = pool();
  fixture.readyAll();
  fixture.focus(3);
  const promoted = fixture.video(3);
  Object.defineProperty(promoted, 'readyState', { configurable: true, value: 4 });
  fireEvent.stalled(promoted);
  promoted.currentTime = 251;
  fireEvent.timeUpdate(promoted);
  fixture.clock.current.sample?.();
  expect(fixture.clock.current.stalled).toBe(false);
  expect(fixture.clock.current.time).toBe(251);
  Object.defineProperty(promoted, 'readyState', { configurable: true, value: 1 });
  fireEvent.waiting(promoted);
  promoted.currentTime = 252;
  fireEvent.timeUpdate(promoted);
  fixture.clock.current.sample?.();
  expect(fixture.clock.current.stalled).toBe(true);
  expect(fixture.clock.current.time).toBe(251);
  Object.defineProperty(promoted, 'readyState', { configurable: true, value: 4 });
  fireEvent.canPlay(promoted);
  expect(fixture.clock.current.stalled).toBe(false);
  expect(fixture.clock.current.time).toBe(252);
});

test('unmount stops and silences all persistent decoders and removes the active sample callback', () => {
  const fixture = pool();
  fixture.readyAll();
  const nodes = FEEDS.map((feed) => fixture.video(feed.id));
  fixture.unmount();
  expect(nodes.every((node) => node.paused && node.muted)).toBe(true);
  expect(fixture.media.current).toBeNull();
  expect(fixture.clock.current.sample).toBeUndefined();
  play.mockClear();
  jest.advanceTimersByTime(60000);
  expect(play).not.toHaveBeenCalled();
});
