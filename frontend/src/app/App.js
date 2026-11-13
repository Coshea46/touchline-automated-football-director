import './styles.css';
import AppShell from './AppShell';
import useTheme from './useTheme';
import useNavigation from './useNavigation';
import WatchView from '../features/watch/WatchView';
import useWatchReplay from '../features/watch/session/useWatchReplay';
import TablesPage from '../features/tables/TablesPage';
import NewsPage from '../features/news/NewsPage';
import { useCallback, useEffect, useState } from 'react';
import StartupExperience from '../features/watch/StartupExperience';

export default function App() {
  const [theme, setTheme] = useTheme();
  const { activePage, navigate } = useNavigation();
  const [startup, setStartup] = useState(process.env.REACT_APP_STARTUP_TUTORIAL !== 'false'
    && (process.env.NODE_ENV === 'production' || process.env.REACT_APP_STARTUP_TUTORIAL === 'true'));
  const [cancelled, setCancelled] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const cancel = useCallback(() => setCancelled(true), []);
  const enter = useCallback(() => setStartup(false), []);
  const replay = useWatchReplay(activePage, startup, cancelled);
  useEffect(() => {
    if (startup && (replay.visitor.phase === 'ended' || replay.sessionState?.state === 'error'
      || ['ai_busy', 'budget_exhausted', 'rate_limited', 'server_unavailable', 'ai_finished'].includes(replay.sessionState?.ai_reason))) cancel();
  }, [startup, replay.visitor.phase, replay.sessionState?.state, replay.sessionState?.ai_reason, cancel]);
  const view = { ...replay, view: { ...replay.view, inferenceBuffering: startup || replay.view.inferenceBuffering } };
  return <><div inert={startup ? true : undefined} aria-hidden={startup ? true : undefined}><AppShell theme={theme} setTheme={setTheme} activePage={activePage} navigate={navigate} announcement={replay.announcement}>
    <main id="matches" className="app-content" tabIndex={-1} onKeyDown={replay.onPlayerKey}>
      <WatchView active={activePage === 'watch'} replay={view} />
      <div id="tables" hidden={activePage !== 'tables'}><TablesPage active={activePage === 'tables'} /></div>
      <div id="news" hidden={activePage !== 'news'}><NewsPage active={activePage === 'news'} /></div>
    </main>
  </AppShell></div>{startup && <StartupExperience key={attempt} ready={replay.view.autoAvailable} onEnter={enter} onCancel={cancel}
    cancelled={cancelled} notice={replay.visitor.notice || replay.modeNotice} onManual={replay.sessionState ? enter : undefined} onRetry={() => {
      replay.visitor.startNewReplay(); setCancelled(false); setAttempt((value) => value + 1);
    }} />}</>;
}
