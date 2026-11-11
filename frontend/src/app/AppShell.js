import { Activity, Moon, Sun } from 'lucide-react';
import { withViewTransition } from './viewTransition';

export default function AppShell({ theme, setTheme, activePage, navigate, announcement, children }) {
  return (
    <div className="app" data-theme={theme}>
      <a className="skip-link" href="#matches" onClick={(event) => { event.preventDefault(); document.getElementById('matches')?.focus(); }}>Skip to content</a>
      <header className="app-header">
        <a className="brand" href="#watch" onClick={(event) => { event.preventDefault(); navigate('watch'); }} aria-label="Touchline home">
          <span>Touchline</span>
        </a>
        <nav className="main-nav" aria-label="Main navigation">
          {[['watch', 'Watch'], ['tables', 'Tables'], ['news', 'News']].map(([id, label]) =>
            <a key={id} href={`#${id}`} aria-current={activePage === id ? 'page' : undefined}
              onClick={(event) => { event.preventDefault(); navigate(id); }}>{label}</a>)}
        </nav>
        <div className="header-actions">
          <button className="theme-toggle icon-button" onClick={() => withViewTransition(() => setTheme(theme === 'dark' ? 'light' : 'dark'))}
            aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}>
            {theme === 'dark' ? <Sun size={20} aria-hidden="true" /> : <Moon size={20} aria-hidden="true" />}
          </button>
        </div>
      </header>
      {children}
      <div className={`selection-toast ${announcement ? 'is-visible' : ''}`} role="status" aria-live="polite">
        {announcement && <><Activity size={18} aria-hidden="true" /><span><strong>Touchline selected</strong><span>{announcement}</span></span></>}
      </div>
    </div>
  );
}
