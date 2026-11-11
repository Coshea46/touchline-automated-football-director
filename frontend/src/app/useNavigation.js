import { useCallback, useEffect, useRef, useState } from 'react';
import { withViewTransition } from './viewTransition';

const PAGES = ['watch', 'tables', 'news'];
const pageFromHash = () => PAGES.includes(window.location.hash.slice(1).toLowerCase()) ? window.location.hash.slice(1).toLowerCase() : 'watch';

export default function useNavigation() {
  const [activePage, setActivePage] = useState(pageFromHash);
  const navigation = useRef(0);
  const navigate = useCallback((page, pushHistory = true) => {
    const next = PAGES.includes(page) ? page : 'watch';
    const request = ++navigation.current;
    if (pushHistory && window.location.hash !== `#${next}`) window.history.pushState(null, '', `#${next}`);
    withViewTransition(() => { if (request === navigation.current) setActivePage(next); });
  }, []);

  useEffect(() => {
    const normalize = () => {
      const page = pageFromHash();
      if (window.location.hash && window.location.hash !== `#${page}`) window.history.replaceState(null, '', `#${page}`);
      return page;
    };
    normalize();
    const onRoute = () => navigate(normalize(), false);
    window.addEventListener('hashchange', onRoute);
    window.addEventListener('popstate', onRoute);
    return () => { window.removeEventListener('hashchange', onRoute); window.removeEventListener('popstate', onRoute); };
  }, [navigate]);

  return { activePage, navigate };
}
