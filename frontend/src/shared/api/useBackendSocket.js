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
export default function useBackendSocket(onMessage, url = WS_URL, sendRef) {
  const [status, setStatus] = useState(url ? 'connecting' : 'disabled');
  const handler = useRef(onMessage);
  useEffect(() => { handler.current = onMessage; });

  useEffect(() => {
    if (!url || typeof WebSocket === 'undefined') {
      if (sendRef) sendRef.current = () => false;
      setStatus('disabled');
      return undefined;
    }
    let socket;
    let retryTimer;
    let attempts = 0;
    let disposed = false;
    if (sendRef) sendRef.current = (message) => {
      if (disposed || socket?.readyState !== 1) return false;
      try { socket.send(JSON.stringify(message)); return true; } catch { return false; }
    };

    const connect = () => {
      if (disposed) return;
      if (attempts === 0) setStatus('connecting');
      socket = new WebSocket(url);
      const currentSocket = socket;
      socket.onopen = () => {
        if (disposed || currentSocket !== socket) return;
        attempts = 0;
        setStatus('connected');
        window.dispatchEvent(new Event(BACKEND_CONNECTED_EVENT));
      };
      socket.onmessage = (event) => {
        if (disposed || currentSocket !== socket) return;
        let message;
        try { message = JSON.parse(event.data); } catch { return; }
        if (message && typeof message.type === 'string') handler.current(message);
      };
      // onerror is always followed by onclose, so retrying here covers both.
      socket.onclose = () => {
        if (disposed || currentSocket !== socket) return;
        setStatus('offline');
        retryTimer = setTimeout(connect, Math.min(30000, 1000 * 2 ** attempts++));
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
  }, [url, sendRef]);

  return status;
}
