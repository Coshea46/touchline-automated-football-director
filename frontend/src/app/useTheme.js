import { useEffect, useState } from 'react';

export default function useTheme() {
  const [theme, setTheme] = useState(() => {
    try { return localStorage.getItem('match-switcher-theme') === 'dark' ? 'dark' : 'light'; }
    catch { return 'light'; }
  });
  useEffect(() => {
    try { localStorage.setItem('match-switcher-theme', theme); } catch {}
    document.documentElement.dataset.theme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = theme === 'dark' ? '#111b2e' : '#eef2f9';
  }, [theme]);

  return [theme, setTheme];
}
