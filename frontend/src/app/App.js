import './styles.css';
import AppShell from './AppShell';
import useTheme from './useTheme';
import useNavigation from './useNavigation';
import WatchView from '../features/watch/WatchView';
import useWatchReplay from '../features/watch/session/useWatchReplay';
import TablesPage from '../features/tables/TablesPage';
import NewsPage from '../features/news/NewsPage';

export default function App() {
  const [theme, setTheme] = useTheme();
  const { activePage, navigate } = useNavigation();
  const replay = useWatchReplay(activePage);
  return <AppShell theme={theme} setTheme={setTheme} activePage={activePage} navigate={navigate} announcement={replay.announcement}>
    <main id="matches" className="app-content" tabIndex={-1} onKeyDown={replay.onPlayerKey}>
      <WatchView active={activePage === 'watch'} replay={replay} />
      <div id="tables" hidden={activePage !== 'tables'}><TablesPage active={activePage === 'tables'} /></div>
      <div id="news" hidden={activePage !== 'news'}><NewsPage active={activePage === 'news'} /></div>
    </main>
  </AppShell>;
}
