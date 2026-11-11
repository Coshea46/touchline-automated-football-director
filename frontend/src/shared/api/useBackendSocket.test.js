import { act, renderHook } from '@testing-library/react';
import useBackendSocket from './useBackendSocket';

const socket = () => global.FakeWebSocket.instances.at(-1);

test('drops disconnected outbound messages and ignores old sockets after reconnect/cleanup', () => {
  jest.useFakeTimers();
  try {
    const handler = jest.fn();
    const outgoing = { current: null };
    const { unmount } = renderHook(() => useBackendSocket(handler, 'ws://test', outgoing));
    expect(outgoing.current({ type: 'playback' })).toBe(false);
    act(() => socket().open());
    expect(outgoing.current({ type: 'playback', position: 10 })).toBe(true);
    expect(socket().sent).toEqual([{ type: 'playback', position: 10 }]);
    const previous = socket();
    act(() => previous.drop());
    expect(outgoing.current({ type: 'playback', position: 999 })).toBe(false);
    act(() => { jest.advanceTimersByTime(1000); socket().open(); });
    expect(socket().sent).toEqual([]);
    act(() => { previous.receive({ type: 'old' }); socket().receive({ type: 'fresh' }); });
    expect(handler.mock.calls).toEqual([[{ type: 'fresh' }]]);
    const count = global.FakeWebSocket.instances.length;
    unmount();
    expect(outgoing.current({ type: 'playback' })).toBe(false);
    act(() => { socket().receive({ type: 'late' }); jest.advanceTimersByTime(60000); });
    expect(handler).toHaveBeenCalledTimes(1);
    expect(global.FakeWebSocket.instances).toHaveLength(count);
  } finally { jest.useRealTimers(); }
});

test('reconnect backoff is bounded at thirty seconds', () => {
  jest.useFakeTimers();
  try {
    const { unmount } = renderHook(() => useBackendSocket(() => {}, 'ws://test'));
    [1000, 2000, 4000, 8000, 16000, 30000, 30000].forEach((delay) => {
      act(() => socket().drop());
      const count = global.FakeWebSocket.instances.length;
      act(() => jest.advanceTimersByTime(delay - 1));
      expect(global.FakeWebSocket.instances).toHaveLength(count);
      act(() => jest.advanceTimersByTime(1));
      expect(global.FakeWebSocket.instances).toHaveLength(count + 1);
    });
    unmount();
  } finally { jest.useRealTimers(); }
});

test('an explicitly disabled backend creates no connection', () => {
  const outgoing = { current: null };
  const { result } = renderHook(() => useBackendSocket(() => {}, '', outgoing));
  expect(result.current).toBe('disabled');
  expect(global.FakeWebSocket.instances).toHaveLength(0);
  expect(outgoing.current({ type: 'playback' })).toBe(false);
});

test('only a current successful connection wakes failed data requests', () => {
  jest.useFakeTimers();
  const recovered = jest.fn();
  window.addEventListener('matchpulse-backend-connected', recovered);
  try {
    const { unmount } = renderHook(() => useBackendSocket(() => {}, 'ws://test'));
    const first = socket();
    act(() => first.open());
    expect(recovered).toHaveBeenCalledTimes(1);
    act(() => first.drop());
    expect(recovered).toHaveBeenCalledTimes(1);
    act(() => jest.advanceTimersByTime(1000));
    act(() => first.open());
    expect(recovered).toHaveBeenCalledTimes(1);
    act(() => socket().open());
    expect(recovered).toHaveBeenCalledTimes(2);
    unmount();
    act(() => socket().open());
    expect(recovered).toHaveBeenCalledTimes(2);
  } finally {
    window.removeEventListener('matchpulse-backend-connected', recovered);
    jest.useRealTimers();
  }
});
