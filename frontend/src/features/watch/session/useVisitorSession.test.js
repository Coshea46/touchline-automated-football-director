import { useRef } from 'react';
import { act, renderHook } from '@testing-library/react';
import useBackendSocket from '../../../shared/api/useBackendSocket';
import useVisitorSession, { VISITOR_STORAGE_KEY } from './useVisitorSession';

const endpoint = 'wss://backend.example/ws';
const auth = (resumed = false) => ({ type: 'session_auth', session_id: 'session_1234567890abcdef',
  resume_token: 'private_resume_1234567890abcdefghijklmnop', resumed, expires_at: Date.now() / 1000 + 1200 });
const socket = () => global.FakeWebSocket.instances.at(-1);
const drop = (code = 1006) => { socket().readyState = 3; socket().onclose?.({ code }); };

function connected() {
  const handler = jest.fn();
  const view = renderHook(() => {
    const visitor = useVisitorSession(true);
    const outgoing = useRef(null);
    const status = useBackendSocket(handler, endpoint, outgoing, visitor.socketOptions);
    return { visitor, outgoing, status };
  });
  return { ...view, handler };
}

beforeEach(() => { sessionStorage.clear(); localStorage.clear(); jest.useFakeTimers(); });
afterEach(() => { jest.restoreAllMocks(); jest.useRealTimers(); });

test('hello is first, controls wait for private auth, and capability never enters URLs or ordinary handlers', () => {
  const view = connected();
  act(() => socket().open());
  expect(socket().url).toBe(endpoint);
  expect(socket().sent).toEqual([{ type: 'hello' }]);
  expect(view.result.current.outgoing.current({ type: 'auto', enabled: true })).toBe(false);
  act(() => socket().receive({ type: 'session', session_id: 'premature' }));
  expect(view.handler).not.toHaveBeenCalled();
  act(() => socket().receive(auth()));
  expect(view.result.current.status).toBe('connected');
  expect(view.result.current.visitor.phase).toBe('ready');
  expect(view.handler).not.toHaveBeenCalled();
  const stored = JSON.parse(sessionStorage.getItem(VISITOR_STORAGE_KEY));
  expect(stored).toEqual({ session_id: auth().session_id, resume_token: auth().resume_token, expires_at: auth().expires_at });
  expect(localStorage.length).toBe(0);
  expect(view.result.current.outgoing.current({ type: 'auto', enabled: true })).toBe(true);
  expect(socket().sent[1]).toEqual({ type: 'auto', enabled: true });
});

test('reconnect resumes the same per-tab capability without creating a new visitor', () => {
  const view = connected();
  act(() => { socket().open(); socket().receive(auth()); });
  act(() => drop());
  act(() => jest.advanceTimersByTime(1000));
  act(() => socket().open());
  expect(socket().sent).toEqual([{ type: 'hello', session_id: auth().session_id, resume_token: auth().resume_token }]);
  expect(view.result.current.outgoing.current({ type: 'playback', position: 25 })).toBe(false);
  act(() => socket().receive(auth(true)));
  expect(view.result.current.status).toBe('connected');
});

test.each([4403, 4404, 4409])('terminal resume close %s keeps credentials and offers only explicit fresh creation', (code) => {
  const view = connected();
  act(() => { socket().open(); socket().receive(auth()); });
  const stored = sessionStorage.getItem(VISITOR_STORAGE_KEY);
  act(() => drop(code));
  expect(view.result.current.visitor.phase).toBe('ended');
  act(() => jest.advanceTimersByTime(120000));
  expect(global.FakeWebSocket.instances).toHaveLength(1);
  expect(sessionStorage.getItem(VISITOR_STORAGE_KEY)).toBe(stored);
  act(() => view.result.current.visitor.restart());
  expect(sessionStorage.getItem(VISITOR_STORAGE_KEY)).toBeNull();
  act(() => socket().open());
  expect(socket().sent).toEqual([{ type: 'hello' }]);
});

test('server rejection of a stored capability never silently adopts a fresh session', () => {
  sessionStorage.setItem(VISITOR_STORAGE_KEY, JSON.stringify(auth()));
  const view = connected();
  act(() => socket().open());
  act(() => socket().receive({ ...auth(false), session_id: 'replacement_1234567890abcdef' }));
  expect(view.result.current.visitor.phase).toBe('ended');
  expect(JSON.parse(sessionStorage.getItem(VISITOR_STORAGE_KEY)).session_id).toBe(auth().session_id);
  act(() => jest.advanceTimersByTime(60000));
  expect(global.FakeWebSocket.instances).toHaveLength(1);
});

test('malformed saved capability requires explicit fresh replay instead of an implicit new hello', () => {
  sessionStorage.setItem(VISITOR_STORAGE_KEY, '{malformed');
  const view = connected();
  expect(view.result.current.visitor.phase).toBe('ended');
  expect(global.FakeWebSocket.instances).toHaveLength(0);
  act(() => view.result.current.visitor.restart());
  act(() => socket().open());
  expect(socket().sent).toEqual([{ type: 'hello' }]);
});

test('admission rate limits honor retry_after and retain the same resume capability', () => {
  sessionStorage.setItem(VISITOR_STORAGE_KEY, JSON.stringify(auth()));
  const view = connected();
  act(() => { socket().open(); socket().receive({ type: 'error', code: 'rate_limited', retry_after: 30 }); drop(4429); });
  expect(view.result.current.visitor.phase).toBe('waiting');
  expect(view.result.current.visitor.retryAt).toBe(Date.now() + 30000);
  act(() => jest.advanceTimersByTime(29999));
  expect(global.FakeWebSocket.instances).toHaveLength(1);
  act(() => jest.advanceTimersByTime(1));
  act(() => socket().open());
  expect(socket().sent[0]).toEqual({ type: 'hello', session_id: auth().session_id, resume_token: auth().resume_token });
});

test('blocked browser storage still permits in-memory resume during this page lifetime', () => {
  jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
  jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
  const view = connected();
  act(() => { socket().open(); socket().receive(auth()); drop(); });
  act(() => jest.advanceTimersByTime(1000));
  act(() => socket().open());
  expect(socket().sent[0]).toEqual({ type: 'hello', session_id: auth().session_id, resume_token: auth().resume_token });
  expect(view.result.current.visitor.notice).not.toContain(auth().resume_token);
});
