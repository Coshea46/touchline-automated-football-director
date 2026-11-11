import { flushSync } from 'react-dom';

const ignore = () => {};

// Runs a state update inside a View Transition so the browser animates
// between the old and new layout. Falls back to an instant update when the
// API is missing, the user prefers reduced motion, or the tab is hidden
// (e.g. the backend switches matches while the user is on another tab).
export function withViewTransition(update) {
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  if (typeof document.startViewTransition !== 'function' || reduce || document.hidden) {
    update();
    return;
  }
  const transition = document.startViewTransition(() => flushSync(update));
  // The browser may still abort (tab hidden mid-way, a newer switch). The update
  // itself has still been applied, so the rejected promises are expected noise.
  transition.ready?.catch(ignore);
  transition.finished?.catch(ignore);
  transition.updateCallbackDone?.catch(ignore);
}
