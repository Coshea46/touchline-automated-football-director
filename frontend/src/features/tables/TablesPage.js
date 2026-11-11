import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Search, Trophy } from 'lucide-react';
import { apiGet, isPlatformConnectionError, safeWebUrl, updatedLabel, useApiRecovery } from '../../shared/api/platformApi';
import { searchCatalogue } from './leagueSearch';
import DataNotice from '../../shared/components/DataNotice';

const stat = (value) => Number.isFinite(value) ? value : '—';
const leaguePreference = () => { try { return localStorage.getItem('matchpulse-league') || 'PL'; } catch { return 'PL'; } };

export default function TablesPage({ active }) {
  const [catalogue, setCatalogue] = useState(null);
  const [competition, setCompetition] = useState(leaguePreference);
  const [query, setQuery] = useState('');
  const [highlight, setHighlight] = useState(null);
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [catalogueNetworkFailure, setCatalogueNetworkFailure] = useState(false);
  const [standingsNetworkFailure, setStandingsNetworkFailure] = useState(false);
  const recovery = useApiRecovery(active, catalogueNetworkFailure || standingsNetworkFailure, loading);
  const lastAttempt = useRef(0);
  useEffect(() => {
    if (!active) return undefined;
    let cancelled = false;
    let enrichedApplied = false;
    // Competition aliases are available immediately; real club rosters load in
    // the background from the provider's cached standings, without guesses.
    apiGet('/api/catalog').then((value) => { if (!cancelled && !enrichedApplied) setCatalogue(value); }).catch(() => {});
    apiGet('/api/catalog', { load_teams: 1 }).then((value) => {
      if (!cancelled) { enrichedApplied = true; setCatalogue(value); setCatalogueNetworkFailure(false); }
    }).catch((reason) => {
      if (!cancelled) { setCatalogueNetworkFailure(isPlatformConnectionError(reason)); setError(reason.message); }
    });
    return () => { cancelled = true; };
  }, [active, recovery]);
  useEffect(() => {
    if (!active) return undefined;
    let cancelled = false;
    setLoading(true);
    setError('');
    setData(null);
    const refresh = attempt !== lastAttempt.current;
    lastAttempt.current = attempt;
    apiGet('/api/standings', { competition }, refresh).then((value) => {
      if (!cancelled) { setData(value); setStandingsNetworkFailure(false); }
    }).catch((reason) => {
      if (!cancelled) { setStandingsNetworkFailure(isPlatformConnectionError(reason)); setError(reason.message); }
    }).finally(() => { if (!cancelled) setLoading(false); });
    try { localStorage.setItem('matchpulse-league', competition); } catch {}
    return () => { cancelled = true; };
  }, [active, competition, attempt, recovery]);
  const searchIndex = useMemo(() => {
    const teams = [...(catalogue?.teams || [])];
    for (const table of data?.tables || []) {
      for (const row of table.rows || []) {
        if (!teams.some((team) => String(team.id) === String(row.team.id))) teams.push({ ...row.team,
          aliases: [row.team.short_name].filter(Boolean), competition_code: data.competition?.code || competition });
      }
    }
    return { ...catalogue, teams };
  }, [catalogue, data, competition]);
  const results = useMemo(() => searchCatalogue(searchIndex, query), [searchIndex, query]);
  const choose = useCallback((item) => { setCompetition(item.competition_code); setHighlight(item.kind === 'team' ? item.team_id : null); setQuery(''); }, []);
  const best = results.filter((item) => item.confidence >= (results[0]?.confidence || 0) - .025);
  const ambiguous = best.filter((item) => item.kind === 'team').length > 1;
  const tables = Array.isArray(data?.tables) ? data.tables : [];
  const selected = catalogue?.competitions?.find((item) => item.code === competition);
  const crest = safeWebUrl(data?.competition?.emblem || selected?.emblem);
  return <section className="content-page platform-page" aria-labelledby="tables-title">
    <header className="platform-page-header"><h1 id="tables-title">League tables</h1>
      <p className="page-lead">Current-season standings, separate from the replays in Watch.</p></header>
    <div className="platform-toolbar"><form className="platform-search" onSubmit={(event) => { event.preventDefault(); if (best.length === 1) choose(best[0]); }}>
      <Search size={19} aria-hidden="true" /><label className="sr-only" htmlFor="league-search">Find a league or club</label>
      <input id="league-search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search leagues or clubs" autoComplete="off" />
      <button type="submit" className="primary-button">Find</button>
    </form><label className="competition-select">Competition<select aria-label="Competition" value={competition} onChange={(event) => { setCompetition(event.target.value); setHighlight(null); }}>
      {(catalogue?.competitions || [{ code: 'PL', name: 'Premier League' }]).map((item) => <option key={item.code} value={item.code}>{item.name}</option>)}
    </select></label></div>
    {query && <div className="catalogue-results" role="region" aria-label="League and club search results">
      {ambiguous && <p>More than one club matches. Choose the club you mean.</p>}
      {!results.length && <p>{!catalogue ? 'Connecting to the league catalogue…' : catalogue.team_catalog_status === 'missing_key'
        ? 'Connect standings data to search current club positions. Competition aliases remain searchable.' : 'No matching league or club found. Try its full name.'}</p>}
      {results.map((item) => <button className="catalogue-result" key={`${item.kind}-${item.team_id || item.competition_code}`} onClick={() => choose(item)}>
        <span>{item.name}</span><span>{item.kind === 'team' ? catalogue?.competitions?.find((entry) => entry.code === item.competition_code)?.name || item.competition_code : 'Competition'}</span>
      </button>)}
    </div>}
    {loading && <div className="data-loading" role="status">Loading standings…</div>}
    {!loading && (error || (data && data.status !== 'ok' && !tables.length)) && <DataNotice data={data} error={error} kind="standings" retry={() => setAttempt((value) => value + 1)} />}
    {!loading && tables.length > 0 && <>
      {(data.stale || data.status !== 'ok') && <p className="cache-banner" role="status">Showing cached provider standings. {data.message || 'Fresh data is temporarily unavailable.'}</p>}
      <header className="standings-heading">{crest ? <img src={crest} alt="" onError={(event) => { event.currentTarget.style.display = 'none'; }} /> : <Trophy size={28} aria-hidden="true" />}
        <div><h2>{data.competition?.name || selected?.name || competition}</h2><p>{data.season?.start_date || data.season?.startDate || 'Season'}{data.season?.end_date || data.season?.endDate ? ` – ${data.season.end_date || data.season.endDate}` : ''}</p></div>
        <span className={data.stale ? 'freshness is-stale' : 'freshness'}>{data.stale ? 'Cached data · ' : ''}{updatedLabel(data.fetched_at)}</span></header>
      {tables.map((table, index) => <div className="standings-block" key={`${table.stage}-${table.group}-${table.type}-${index}`}>
        {(table.group || tables.length > 1) && <h3>{[table.group, table.stage, table.type].filter(Boolean).map((value) => String(value).replaceAll('_', ' ').toLowerCase()).join(' · ')}</h3>}
        <div className="standings-scroll"><table className="standings-table"><caption className="sr-only">{data.competition?.name || competition} standings{table.group ? ` ${table.group}` : ''}</caption>
          <thead><tr><th scope="col">#</th><th scope="col">Club</th>{[['P', 'Matches played'], ['W', 'Wins'], ['D', 'Draws'], ['L', 'Losses'], ['GF', 'Goals for'], ['GA', 'Goals against'], ['GD', 'Goal difference'], ['Pts', 'Points']].map(([short, long]) => <th key={short} scope="col"><abbr title={long}>{short}</abbr></th>)}{table.rows?.some((row) => row.form) && <th scope="col">Form</th>}</tr></thead>
          <tbody>{(table.rows || []).map((row) => <tr key={row.team.id} className={highlight != null && String(highlight) === String(row.team.id) ? 'highlighted-club' : undefined}>
            <td>{stat(row.position)}</td><th scope="row">{safeWebUrl(row.team.crest) && <img src={row.team.crest} alt="" loading="lazy" onError={(event) => { event.currentTarget.style.display = 'none'; }} />}<span>{row.team.name}</span></th>
            {[row.played, row.won, row.drawn, row.lost, row.goals_for, row.goals_against, row.goal_difference, row.points].map((value, field) => <td key={field} className={field === 7 ? 'standings-points' : undefined}>{stat(value)}</td>)}
            {table.rows?.some((entry) => entry.form) && <td>{row.form || '—'}</td>}
          </tr>)}</tbody></table></div>
        {!table.rows?.length && <p className="section-note">No standings rows are available for this stage.</p>}
      </div>)}
      <p className="section-note">— means the provider did not supply that statistic. {safeWebUrl(data.attribution?.url)
        ? <a href={data.attribution.url} target="_blank" rel="noreferrer">{data.attribution.text}</a>
        : typeof data.attribution === 'string' ? data.attribution : 'Standings supplied by football-data.org.'}</p>
    </>}
    {!loading && data?.status === 'ok' && !tables.length && <p className="empty-state">No league or group standings are available for this competition.</p>}
  </section>;
}
