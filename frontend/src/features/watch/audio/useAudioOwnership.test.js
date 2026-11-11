import React, { useLayoutEffect, useRef } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { createAudioOwnership } from './audioOwnership';
import useAudioOwnership from './useAudioOwnership';

jest.mock('./audioOwnership', () => ({ createAudioOwnership: jest.fn() }));

let broker;
let changes;

beforeEach(() => {
  createAudioOwnership.mockReset();
  createAudioOwnership.mockImplementation(({ onChange }) => {
    let owned = false;
    changes = (next) => { owned = next.owned; onChange({ pending: false, explicit: false, ...next }); };
    broker = {
      get owned() { return owned; },
      request: jest.fn(({ explicit }) => {
        changes({ owned: true, status: 'owned', explicit });
        return Promise.resolve(true);
      }),
      release: jest.fn(() => changes({ owned: false, status: 'other-tab' })),
      dispose: jest.fn(() => changes({ owned: false, status: 'unavailable' })),
    };
    return broker;
  });
});

function Pool({ focused = 'cam-1', clock, enabled = true }) {
  const first = useRef(null);
  const second = useRef(null);
  const media = useRef(null);
  // The real pool's child layout effect promotes its native node before this
  // parent hook binds guards. No media element is replaced during promotion.
  useLayoutEffect(() => { media.current = focused === 'cam-1' ? first.current : second.current; }, [focused]);
  const ownership = useAudioOwnership({ media, clock, sourceId: focused, enabled });
  return <>
    <video ref={first} data-testid="cam-1" muted />
    <video ref={second} data-testid="cam-2" muted />
    <button onClick={() => { void ownership.requestAudio(); }}>Request audio</button>
    <output>{ownership.status}</output>
  </>;
}

function trackMute(video, name, writes) {
  let muted = video.muted;
  Object.defineProperty(video, 'muted', {
    configurable: true,
    get: () => muted,
    set: (next) => { muted = next; writes.push([name, next]); },
  });
}

test('promotion mutes outgoing before incoming unmute while retaining the same tab lease', () => {
  const clock = { current: { muted: false } };
  const { rerender } = render(<Pool clock={clock} />);
  const first = screen.getByTestId('cam-1');
  const second = screen.getByTestId('cam-2');
  expect(first.muted).toBe(false);
  expect(second.muted).toBe(true);
  const writes = [];
  trackMute(first, 'old', writes);
  trackMute(second, 'new', writes);
  rerender(<Pool focused="cam-2" clock={clock} />);
  expect(screen.getByTestId('cam-1')).toBe(first);
  expect(screen.getByTestId('cam-2')).toBe(second);
  expect(first.muted).toBe(true);
  expect(second.muted).toBe(false);
  expect(first.matchpulseAudioOwned).toBe(false);
  expect(second.matchpulseAudioOwned).toBe(true);
  expect(writes.findIndex(([name, mute]) => name === 'old' && mute)).toBeLessThan(
    writes.findIndex(([name, mute]) => name === 'new' && !mute));
  rerender(<Pool focused="cam-1" clock={clock} />);
  expect(createAudioOwnership).toHaveBeenCalledTimes(1);
  expect(broker.request).toHaveBeenCalledTimes(1);
  expect(broker.dispose).not.toHaveBeenCalled();
  expect(clock.current.muted).toBe(false);
});

test('another tab revokes the newly promoted node and its gesture can reacquire audio', () => {
  const clock = { current: { muted: false } };
  const { rerender } = render(<Pool clock={clock} />);
  const first = screen.getByTestId('cam-1');
  const second = screen.getByTestId('cam-2');
  rerender(<Pool focused="cam-2" clock={clock} />);
  const writes = [];
  trackMute(first, 'old', writes);
  trackMute(second, 'new', writes);
  act(() => changes({ owned: false, status: 'other-tab' }));
  expect(second.muted).toBe(true);
  expect(second.matchpulseAudioOwned).toBe(false);
  expect(writes.some(([name]) => name === 'old')).toBe(false);
  expect(screen.getByText('other-tab')).toBeInTheDocument();
  fireEvent.pointerDown(second);
  second.muted = false;
  fireEvent.volumeChange(second);
  expect(broker.request).toHaveBeenLastCalledWith({ explicit: true });
  expect(second.muted).toBe(false);
  expect(first.muted).toBe(true);
  expect(clock.current.muted).toBe(false);
});

test('mute preference survives promotion and unmount mutes the current node before lease disposal', () => {
  const clock = { current: { muted: true } };
  const { rerender, unmount } = render(<Pool clock={clock} />);
  const second = screen.getByTestId('cam-2');
  rerender(<Pool focused="cam-2" clock={clock} />);
  expect(second.muted).toBe(true);
  expect(clock.current.muted).toBe(true);
  fireEvent.click(screen.getByText('Request audio'));
  expect(second.muted).toBe(false);
  expect(clock.current.muted).toBe(false);
  broker.dispose.mockImplementation(() => {
    expect(second.muted).toBe(true);
    expect(second.matchpulseAudioOwned).toBe(false);
  });
  unmount();
  expect(broker.dispose).toHaveBeenCalledTimes(1);
  expect(second.muted).toBe(true);
});

test('a disabled backend role cannot acquire or unmute audio, including a delayed grant', () => {
  const clock = { current: { muted: false } };
  const { rerender } = render(<Pool clock={clock} enabled={false} />);
  const first = screen.getByTestId('cam-1');
  expect(broker.request).not.toHaveBeenCalled();
  expect(first.muted).toBe(true);
  fireEvent.click(screen.getByText('Request audio'));
  fireEvent.pointerDown(first);
  act(() => { first.muted = false; fireEvent.volumeChange(first); });
  expect(first.muted).toBe(true);
  expect(broker.request).not.toHaveBeenCalled();
  rerender(<Pool clock={clock} enabled />);
  expect(broker.request).toHaveBeenCalledWith({ explicit: false });
  expect(first.muted).toBe(false);
  rerender(<Pool clock={clock} enabled={false} />);
  expect(first.muted).toBe(true);
  expect(first.matchpulseAudioOwned).toBe(false);
  clock.current.muted = true;
  act(() => changes({ owned: true, explicit: true, status: 'owned' }));
  expect(first.muted).toBe(true);
  expect(first.matchpulseAudioOwned).toBe(false);
  expect(clock.current.muted).toBe(true);
  expect(screen.getByText('other-tab')).toBeInTheDocument();
});
