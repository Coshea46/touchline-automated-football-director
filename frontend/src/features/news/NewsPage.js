import { useEffect, useRef, useState } from 'react';
import { ArrowUpRight, Newspaper, Search } from 'lucide-react';
import { apiGet, isPlatformConnectionError, safeWebUrl, updatedLabel, useApiRecovery } from '../../shared/api/platformApi';
import DataNotice from '../../shared/components/DataNotice';

const TOPICS = [['football', 'All football'], ['premier-league', 'Premier League'], ['european', 'European football'],
  ['transfers', 'Transfers'], ['champions-league', 'Champions League']];
const publicationDate = (value) => Number.isFinite(Date.parse(value))
  ? new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : 'Publication time unavailable';

function Article({ article }) {
  const url = safeWebUrl(article.url);
  const image = safeWebUrl(article.image_url);
  return <article className="news-card">
    {image && <img className="news-image" src={image} alt={article.image_alt || ''} loading="lazy" onError={(event) => { event.currentTarget.style.display = 'none'; }} />}
    <div className="news-card-body"><span className="news-publisher">{article.publisher || 'The Guardian'}</span>
      <h2><a href={url} target="_blank" rel="noreferrer">{article.title}</a></h2>
      {article.excerpt && <p className="news-excerpt">{article.excerpt}</p>}
      <div className="article-byline">{article.byline && <span>{article.byline}</span>}<time dateTime={article.published_at}>{publicationDate(article.published_at)}</time></div>
      {article.copyright && <p className="article-copyright">{article.copyright}</p>}
      <a className="article-link" href={url} target="_blank" rel="noreferrer">Read at {article.publisher || 'The Guardian'} <ArrowUpRight size={15} aria-hidden="true" /></a>
    </div>
  </article>;
}

export default function NewsPage({ active }) {
  const [topic, setTopic] = useState('football');
  const [draft, setDraft] = useState('');
  const [query, setQuery] = useState('');
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [networkFailure, setNetworkFailure] = useState(false);
  const recovery = useApiRecovery(active, networkFailure, loading);
  const lastAttempt = useRef(0);
  useEffect(() => {
    if (!active) return undefined;
    let cancelled = false;
    setLoading(true); setError(''); setData(null);
    const refresh = attempt !== lastAttempt.current;
    lastAttempt.current = attempt;
    apiGet('/api/news', { topic, q: query }, refresh).then((value) => {
      if (!cancelled) { setData(value); setNetworkFailure(false); }
    }).catch((reason) => {
      if (!cancelled) { setNetworkFailure(isPlatformConnectionError(reason)); setError(reason.message); }
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [active, topic, query, attempt, recovery]);
  useEffect(() => {
    const expires = Number.isFinite(data?.content_expires_at) ? data.content_expires_at
      : Number.isFinite(data?.fetched_at) ? data.fetched_at + 24 * 60 * 60 : null;
    if (expires === null) return undefined;
    const remaining = expires * 1000 - Date.now();
    const timer = setTimeout(() => { setData(null); setError('The publisher cache has expired. Refresh to request current article metadata.'); }, Math.max(0, remaining));
    return () => clearTimeout(timer);
  }, [data]);
  const expires = Number.isFinite(data?.content_expires_at) ? data.content_expires_at
    : Number.isFinite(data?.fetched_at) ? data.fetched_at + 24 * 60 * 60 : null;
  const expired = expires !== null && expires <= Date.now() / 1000;
  const seen = new Set();
  const articles = (!expired && Array.isArray(data?.articles) ? data.articles : []).filter((article) => {
    const url = safeWebUrl(article.url);
    if (!url || typeof article.title !== 'string' || seen.has(article.id || url)) return false;
    seen.add(article.id || url); return true;
  }).sort((a, b) => (Date.parse(b.published_at) || 0) - (Date.parse(a.published_at) || 0));
  const attribution = data?.attribution;
  const logo = safeWebUrl(attribution?.logo_url);
  const publisher = safeWebUrl(attribution?.url) || 'https://www.theguardian.com/football';
  return <section className="content-page platform-page" aria-labelledby="news-title">
    <header className="platform-page-header"><h1 id="news-title">Football news</h1></header>
    <form className="platform-search news-search" onSubmit={(event) => { event.preventDefault(); setQuery(draft.trim()); }}>
      <Search size={19} aria-hidden="true" /><label className="sr-only" htmlFor="news-search">Search football news</label>
      <input id="news-search" maxLength={120} value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="Search football stories" />
      <button className="primary-button" type="submit">Search</button>
    </form>
    <div className="topic-filters" role="group" aria-label="Football news topics">{TOPICS.map(([id, name]) => <button key={id} aria-pressed={topic === id} onClick={() => setTopic(id)}>{name}</button>)}</div>
    {loading && <div className="data-loading" role="status">Loading news…</div>}
    {!loading && (error || (data && data.status !== 'ok' && !articles.length)) && <DataNotice data={data} error={error} kind="news" retry={() => setAttempt((value) => value + 1)} />}
    {!loading && data && articles.length > 0 && <>
      <div className="publisher-attribution"><a href={publisher} target="_blank" rel="noreferrer">{logo ? <img src={logo} alt="Powered by The Guardian" /> : <span>Powered by The Guardian</span>}</a>
        <span className={data.stale ? 'freshness is-stale' : 'freshness'}>{data.stale ? 'Cached headlines · ' : ''}{updatedLabel(data.fetched_at)}</span></div>
      {(data.stale || data.status !== 'ok') && <p className="cache-banner" role="status">These are cached article details, not a live news update. {data.message || ''}</p>}
      <div className="news-grid">{articles.map((article) => <Article key={article.id || article.url} article={article} />)}</div>
    </>}
    {!loading && data?.status === 'ok' && !articles.length && <div className="empty-state"><Newspaper size={28} aria-hidden="true" /><h2>No articles found</h2><p>Try another topic or search.</p></div>}
  </section>;
}
