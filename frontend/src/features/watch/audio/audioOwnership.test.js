import { act, fireEvent, render, screen } from '@testing-library/react';
import { StrictMode, useRef } from 'react';
import { AUDIO_LOCK, AUDIO_REQUEST_TIMEOUT_MS, createAudioOwnership } from './audioOwnership';
import useAudioOwnership from './useAudioOwnership';
import VideoTile from '../compatibility/VideoTile';

const flush = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve(); };

// A small Web Locks scheduler that retains the exclusive lock until the callback
// promise settles. Invalid signal+ifAvailable combinations fail just like the spec.
function browserWorld({ broadcast = true, storage = true, locking = true } = {}) {
  let held = null;
  const queue = [];
  const contexts = [];
  const channels = new Set();
  const requests = [];
  const drain = () => {
    if (held || !queue.length) return;
    const item = queue.shift();
    held = item;
    item.options.signal?.removeEventListener('abort', item.abort);
    Promise.resolve().then(() => item.callback({ name: AUDIO_LOCK })).then(item.resolve, item.reject).finally(() => {
      if (held === item) held = null;
      drain();
    });
  };
  const locks = {
    request(name, options, callback) {
      requests.push({ name, options });
      if (options.ifAvailable && options.signal) throw new Error('Invalid Web Locks options');
      if (options.steal) throw new Error('Audio must never steal a lock');
      if (options.ifAvailable && (held || queue.length)) return Promise.resolve().then(() => callback(null));
      return new Promise((resolve, reject) => {
        const item = { options, callback, resolve, reject };
        item.abort = () => {
          const index = queue.indexOf(item);
          if (index >= 0) queue.splice(index, 1);
          reject(new DOMException('Aborted', 'AbortError'));
        };
        if (options.signal?.aborted) { item.abort(); return; }
        options.signal?.addEventListener('abort', item.abort, { once: true });
        queue.push(item);
        drain();
      });
    },
  };
  const make = () => {
    const browser = new EventTarget();
    browser.navigator = locking ? { locks } : {};
    if (storage) browser.localStorage = { setItem(key, newValue) {
      contexts.filter((other) => other !== browser).forEach((other) => Promise.resolve().then(() => {
        const event = new Event('storage');
        Object.defineProperties(event, { key: { value: key }, newValue: { value: newValue } });
        other.dispatchEvent(event);
      }));
    } };
    else Object.defineProperty(browser, 'localStorage', { get() { throw new Error('Storage disabled'); } });
    if (broadcast) browser.BroadcastChannel = class extends EventTarget {
      constructor() { super(); channels.add(this); }
      postMessage(data) {
        [...channels].filter((other) => other !== this).forEach((other) => Promise.resolve().then(() => {
          other.dispatchEvent(new MessageEvent('message', { data }));
        }));
      }
      close() { channels.delete(this); }
    };
    contexts.push(browser);
    return browser;
  };
  return { make, requests, locks, get queued() { return queue.length; }, get channels() { return channels.size; } };
}

let originalLocksDescriptor;
let originalBroadcast;
beforeEach(() => {
  originalLocksDescriptor = Object.getOwnPropertyDescriptor(navigator, 'locks');
  originalBroadcast = window.BroadcastChannel;
  jest.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  jest.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(() => Promise.resolve());
});
afterEach(() => {
  jest.restoreAllMocks(); jest.useRealTimers();
  if (originalLocksDescriptor) Object.defineProperty(navigator, 'locks', originalLocksDescriptor);
  else delete navigator.locks;
  window.BroadcastChannel = originalBroadcast;
});

test('new tabs acquire only a free exclusive lock and never steal existing audio', async () => {
  const world = browserWorld();
  const a = createAudioOwnership({ browser: world.make(), id: 'a' });
  const b = createAudioOwnership({ browser: world.make(), id: 'b' });
  expect(await a.request({ explicit: false })).toBe(true);
  expect(await b.request({ explicit: false })).toBe(false);
  expect(a.owned).toBe(true);
  expect(b.owned).toBe(false);
  expect(world.requests.every(({ options }) => options.ifAvailable && !options.signal && !options.steal)).toBe(true);
  a.dispose(); b.dispose(); await flush();
});

test.each([{ broadcast: true, storage: true }, { broadcast: false, storage: true }, { broadcast: true, storage: false }])(
  'explicit activation mutes the old owner before granting the next tab (%j)', async (options) => {
    const world = browserWorld(options);
    const order = [];
    const a = createAudioOwnership({ browser: world.make(), id: 'a', onChange: (state) => { if (!state.owned) order.push('a muted'); } });
    const b = createAudioOwnership({ browser: world.make(), id: 'b', onChange: (state) => { if (state.owned) order.push('b audible'); } });
    await a.request({ explicit: false });
    order.length = 0;
    expect(await b.request()).toBe(true);
    expect(order.indexOf('a muted')).toBeLessThan(order.indexOf('b audible'));
    expect(a.owned).toBe(false);
    expect(b.owned).toBe(true);
    expect(world.requests[1].options.signal).toBeDefined();
    expect(world.requests[1].options.ifAvailable).toBeUndefined();
    a.dispose(); b.dispose(); await flush();
  },
);

test('an unresponsive owner remains exclusive and takeover aborts without a ghost grant', async () => {
  jest.useFakeTimers();
  const world = browserWorld({ broadcast: false, storage: false });
  const a = createAudioOwnership({ browser: world.make(), id: 'a' });
  const states = [];
  const b = createAudioOwnership({ browser: world.make(), id: 'b', onChange: (state) => states.push(state) });
  await a.request({ explicit: false });
  const takeover = b.request();
  await flush();
  expect(world.queued).toBe(1);
  jest.advanceTimersByTime(AUDIO_REQUEST_TIMEOUT_MS);
  expect(await takeover).toBe(false);
  expect(world.queued).toBe(0);
  expect(a.owned).toBe(true);
  a.dispose(); await flush();
  expect(b.owned).toBe(false);
  expect(states.at(-1).status).toBe('other-tab');
  b.dispose();
});

test('a new explicit takeover works within the same coarse clock tick regardless of tab ID order', async () => {
  jest.spyOn(Date, 'now').mockReturnValue(1000);
  const world = browserWorld();
  const previous = createAudioOwnership({ browser: world.make(), id: 'z-previous' });
  const current = createAudioOwnership({ browser: world.make(), id: 'a-current' });
  expect(await previous.request({ explicit: false })).toBe(true);
  expect(await current.request({ explicit: true })).toBe(true);
  expect(previous.owned).toBe(false);
  expect(current.owned).toBe(true);
  previous.dispose(); current.dispose(); await flush();
});

test('dispose aborts queued work and disposed automatic callbacks cannot acquire audio', async () => {
  const world = browserWorld({ broadcast: false, storage: false });
  const a = createAudioOwnership({ browser: world.make(), id: 'a' });
  const abandoned = a.request({ explicit: false });
  a.dispose();
  expect(await abandoned).toBe(false);
  await flush();
  const b = createAudioOwnership({ browser: world.make(), id: 'b' });
  expect(await b.request({ explicit: false })).toBe(true);
  const c = createAudioOwnership({ browser: world.make(), id: 'c' });
  const pending = c.request();
  c.dispose();
  expect(await pending).toBe(false);
  expect(world.queued).toBe(0);
  b.dispose(); await flush();
  expect(world.channels).toBe(0);
});

test('missing Web Locks fails closed instead of guessing audio ownership', async () => {
  const world = browserWorld({ locking: false });
  const states = [];
  const a = createAudioOwnership({ browser: world.make(), onChange: (state) => states.push(state) });
  expect(await a.request({ explicit: false })).toBe(false);
  expect(await a.request()).toBe(false);
  expect(states.at(-1)).toMatchObject({ owned: false, status: 'unavailable' });
  a.dispose();
});

function AudioHarness({ clock }) {
  const media = useRef(null);
  const audio = useAudioOwnership({ media, clock });
  return <><video ref={media} data-testid="audio-video" /><span>{audio.status}</span>
    <button onClick={audio.requestAudio}>Play audio here</button></>;
}

test('hook preserves clock/source state while forcing nonowner media mute and supports explicit takeover', async () => {
  const world = browserWorld();
  const originalLocks = navigator.locks;
  const originalChannel = window.BroadcastChannel;
  Object.defineProperty(navigator, 'locks', { configurable: true, value: world.locks });
  window.BroadcastChannel = world.make().BroadcastChannel;
  const foreign = createAudioOwnership({ browser: world.make(), id: 'foreign' });
  await foreign.request({ explicit: false });
  const clock = { current: { time: 127.25, playing: true, rate: 1.25, muted: false, volume: .7 } };
  const before = { ...clock.current };
  const view = render(<AudioHarness clock={clock} />);
  await act(flush);
  const video = screen.getByTestId('audio-video');
  expect(video.muted).toBe(true);
  expect(clock.current).toEqual(before);
  expect(screen.getByText('other-tab')).toBeInTheDocument();
  video.muted = false;
  fireEvent.volumeChange(video);
  expect(video.muted).toBe(true);
  expect(clock.current).toEqual(before);
  fireEvent.click(screen.getByRole('button', { name: 'Play audio here' }));
  await act(flush);
  expect(video.muted).toBe(false);
  expect(video.matchpulseAudioOwned).toBe(true);
  expect(foreign.owned).toBe(false);
  expect(clock.current).toEqual(before);
  await act(async () => { await foreign.request(); });
  expect(video.muted).toBe(true);
  expect(video.matchpulseAudioOwned).toBe(false);
  expect(clock.current).toEqual(before);
  fireEvent.pointerDown(video);
  act(() => {
    video.muted = false;
    fireEvent.volumeChange(video);
  });
  await act(flush);
  expect(video.muted).toBe(false);
  expect(foreign.owned).toBe(false);
  expect(clock.current).toEqual(before);
  view.unmount(); await flush(); foreign.dispose();
  Object.defineProperty(navigator, 'locks', { configurable: true, value: originalLocks });
  window.BroadcastChannel = originalChannel;
});

test('StrictMode remount owns at most one lock and leaves no pending request on cleanup', async () => {
  const world = browserWorld();
  const originalLocks = navigator.locks;
  Object.defineProperty(navigator, 'locks', { configurable: true, value: world.locks });
  const clock = { current: { time: 40, muted: false } };
  const view = render(<StrictMode><AudioHarness clock={clock} /></StrictMode>);
  await act(flush);
  expect(screen.getByText('owned')).toBeInTheDocument();
  view.unmount(); await flush();
  const next = createAudioOwnership({ browser: world.make(), id: 'next' });
  expect(await next.request({ explicit: false })).toBe(true);
  next.dispose(); await flush();
  Object.defineProperty(navigator, 'locks', { configurable: true, value: originalLocks });
});

test('preview is always muted, including native volume changes, fallback and cleanup', () => {
  const feed = { id: 1, cameraId: 'cam-1', label: 'Test match', url: 'https://example.test/full.mp4', previewUrl: 'https://example.test/preview.mp4' };
  const clock = { current: { time: 120, playing: false, rate: 1 } };
  const view = render(<VideoTile feed={feed} clock={clock} />);
  const video = view.container.querySelector('video');
  expect(video.defaultMuted).toBe(true);
  video.muted = false;
  fireEvent.volumeChange(video);
  expect(video.muted).toBe(true);
  video.muted = false;
  fireEvent.play(video);
  expect(video.muted).toBe(true);
  fireEvent.error(video);
  video.muted = false;
  fireEvent.loadedData(video);
  expect(video.muted).toBe(true);
  view.unmount();
  expect(video.muted).toBe(true);
  expect(HTMLMediaElement.prototype.pause).toHaveBeenCalled();
});
