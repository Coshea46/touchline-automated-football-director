import { useEffect, useRef, useState } from 'react';
import { BACKEND_CONNECTED_EVENT } from './connectionEvents';

// docker-compose sets REACT_APP_WS_URL; plain `npm start` uses touchline.local.
// Set REACT_APP_WS_URL= (empty) to turn the connection off.
export const WS_URL = process.env.REACT_APP_WS_URL ?? 'ws://127.0.0.1:8030';

/**
 * Keeps a WebSocket to the backend open, reconnecting with backoff
 * (1s, 2s, 4s ... up to 30s). Calls onMessage with each parsed JSON message.
 * Returns 'connecting' | 'connected' | 'offline' | 'disabled'.
 */
export default function useBackendSocket(onMessage, url = WS_URL, sendRef, options) {
  const [status, setStatus] = useState(url ? 'connecting' : 'disabled');
  const handler = useRef(onMessage);
  const handshake = useRef(options);
  handshake.current = options;
  const generation = options?.generation;
  const blocked = options?.blocked === true;
  useEffect(() => { handler.current = onMessage; });

  useEffect(() => {
    if (!url || blocked || typeof WebSocket === 'undefined') {
      if (sendRef) sendRef.current = () => false;
      setStatus('disabled');
      return undefined;
    }
    let socket;
    let retryTimer;
    let attempts = 0;
    let disposed = false;
    let authenticated = !handshake.current?.createHello;
    let halted = false;
    let retryAfterMs = 0;
    if (sendRef) sendRef.current = (message) => {
      if (disposed || !authenticated || socket?.readyState !== 1) return false;
      try { socket.send(JSON.stringify(message)); return true; } catch { return false; }
    };

    const connect = () => {
      if (disposed || halted) return;
      if (attempts === 0) setStatus('connecting');
      socket = new WebSocket(url);
      const currentSocket = socket;
      authenticated = !handshake.current?.createHello;
      const ready = () => {
        attempts = 0;
        setStatus('connected');
        window.dispatchEvent(new Event(BACKEND_CONNECTED_EVENT));
      };
      socket.onopen = () => {
        if (disposed || currentSocket !== socket) return;
        if (handshake.current?.createHello) {
          handshake.current.onOpen?.();
          try { socket.send(JSON.stringify(handshake.current.createHello())); }
          catch { socket.close(); }
        } else ready();
      };
      socket.onmessage = (event) => {
        if (disposed || currentSocket !== socket) return;
        let message;
        try { message = JSON.parse(event.data); } catch { return; }
        if (handshake.current?.createHello && message?.type === 'session_auth') {
          if (handshake.current.onAuth?.(message)) { authenticated = true; ready(); }
          else { halted = true; socket.close(); }
          return;
        }
        if (handshake.current?.createHello && message?.type === 'error') {
          const action = handshake.current.onError?.(message, { authenticated });
          if (action?.halt) { halted = true; authenticated = false; setStatus('offline'); socket.close(); return; }
          if (action?.retryAfterMs) retryAfterMs = Math.max(retryAfterMs, action.retryAfterMs);
        }
        if (!authenticated) return;
        if (message && typeof message.type === 'string') handler.current(message);
      };
      // onerror is always followed by onclose, so retrying here covers both.
      socket.onclose = (event) => {
        if (disposed || currentSocket !== socket) return;
        const action = handshake.current?.onClose?.(event);
        if (action?.halt) halted = true;
        if (action?.retryAfterMs) retryAfterMs = Math.max(retryAfterMs, action.retryAfterMs);
        authenticated = false;
        setStatus('offline');
        if (!halted) {
          retryTimer = setTimeout(connect, Math.max(retryAfterMs, Math.min(30000, 1000 * 2 ** attempts++)));
          retryAfterMs = 0;
        }
      };
    };

    connect();
    return () => {
      disposed = true;
      if (sendRef) sendRef.current = () => false;
      clearTimeout(retryTimer);
      socket.onclose = null;
      socket.close();
    };
  }, [url, sendRef, generation, blocked]);

  return status;
}
