import { useEffect, useRef, useState } from 'react';
import { DIRECTOR_MODES, MODE_STORAGE_KEY, preferredMode, readDirector } from './director';

export default function useDirectorControls({ session, send, pendingSelection, pendingAck, transition }) {
  const [director, setDirector] = useState(null);
  const [pendingMode, setPendingMode] = useState(null);
  const [modeNotice, setModeNotice] = useState('');
  const directorRef = useRef(null);
  const modePreference = useRef(preferredMode(null));
  const pendingModeRef = useRef(null);
  const modeRequestSession = useRef(null);

  useEffect(() => {
    if (!pendingMode) return undefined;
    const timer = setTimeout(() => {
      pendingModeRef.current = null;
      setPendingMode(null);
      setModeNotice('The mode change was not confirmed. Please try again.');
    }, 8000);
    return () => clearTimeout(timer);
  }, [pendingMode]);

  const updateDirector = (value, registered) => {
    const next = readDirector(value, registered);
    if (!next) return null;
    if (directorRef.current && directorRef.current.mode !== next.mode && pendingSelection.current?.source === 'auto') {
      transition.current += 1;
      pendingSelection.current = null;
      pendingAck.current = null;
    }
    if (directorRef.current && directorRef.current.mode !== next.mode) pendingAck.current = null;
    directorRef.current = next;
    setDirector(next);
    if (pendingModeRef.current === next.mode) {
      pendingModeRef.current = null;
      modePreference.current = next.mode;
      setPendingMode(null);
      setModeNotice('');
      try { localStorage.setItem(MODE_STORAGE_KEY, next.mode); } catch {}
    }
    return next;
  };

  const requestMode = (mode) => {
    if (!Object.hasOwn(DIRECTOR_MODES, mode)) return;
    pendingAck.current = null;
    if (pendingSelection.current?.source === 'auto') {
      transition.current += 1;
      pendingSelection.current = null;
      pendingAck.current = null;
    }
    const current = session.current;
    if (!current || current.controls_enabled === false || !send.current({ type: 'director_mode', session_id: current.session_id, mode })) {
      setModeNotice(current?.controls_enabled === false ? 'This replay is controlled by another viewer.' : 'Connect to the director to change mode.');
      return;
    }
    pendingModeRef.current = mode;
    setPendingMode(mode);
    setModeNotice('');
  };

  // The session coordinator shares these refs to apply authoritative takeover/reset messages.
  return { director, setDirector, pendingMode, setPendingMode, modeNotice, setModeNotice,
    directorRef, modePreference, pendingModeRef, modeRequestSession, updateDirector, requestMode };
}
