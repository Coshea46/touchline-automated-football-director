import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import App from '../app/App';
import TablesPage from '../features/tables/TablesPage';
import NewsPage from '../features/news/NewsPage';
import CommentaryCaptions from '../features/captions/CommentaryCaptions';
import MainPlayer from '../features/watch/compatibility/MainPlayer';
import { FEEDS, getVideoUrl, registeredFeeds } from '../features/watch/sources/feeds';
import { clearApiCache } from '../shared/api/platformApi';
import { captionAt, readCaptionTrack } from '../features/captions/captions';
import { replayScoreAt } from '../features/watch/sources/metadata';
import { searchCatalogue } from '../features/tables/leagueSearch';
import userEvent from '@testing-library/user-event';

const catalogue = { status: 'ok', competitions: [{ code: 'PL', name: 'Premier League', aliases: ['EPL', 'English league'] },
  { code: 'PD', name: 'Primera Division', aliases: ['La Liga', 'Spanish league'] }], teams: [
  { id: 1, name: 'Arsenal FC', aliases: ['Arsenal', 'Gunners'], competition_code: 'PL' },
  { id: 2, name: 'Manchester City FC', aliases: ['Manchester City', 'Man City'], competition_code: 'PL' },
  { id: 3, name: 'Manchester United FC', aliases: ['Manchester United', 'Man United'], competition_code: 'PL' }] };
const standings = { status: 'ok', competition: { name: 'Premier League' }, attribution: { text: 'Football data provided by football-data.org', url: 'https://www.football-data.org/' }, fetched_at: Date.now() / 1000,
  season: { start_date: '2026-08-01', end_date: '2027-05-30' }, tables: [{ type: 'TOTAL', rows: [
    { position: 1, team: { id: 1, name: 'Arsenal FC' }, played: 10, won: 8, drawn: 1, lost: 1, goals_for: 20, goals_against: 7, goal_difference: 13, points: 25 },
    { position: 2, team: { id: 2, name: 'Manchester City FC' }, played: 10, won: 7, drawn: 2, lost: 1, goals_for: 19, goals_against: 8, goal_difference: 11, points: 23 }] }] };
const reply = (body) => Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
beforeEach(() => { localStorage.clear(); window.history.replaceState(null, '', '/'); clearApiCache(); global.fetch = jest.fn(); });
afterEach(() => { clearApiCache(); delete global.fetch; jest.useRealTimers(); });

test('all seven labels follow the canonical fixtures and custom sources keep their identity', () => {
  expect(FEEDS.map((feed) => feed.label)).toEqual(['Chelsea vs Swansea City', 'Manchester City vs Chelsea',
    'West Bromwich Albion vs Chelsea', 'Chelsea vs Crystal Palace', 'Liverpool vs West Ham United',
    'Manchester City vs Watford', 'Swansea City vs Manchester United']);
  const [custom] = registeredFeeds([{ match_id: 'cam-1', label: 'My camera', url: 'https://example.org/match-1.mp4', source_key: 'match-1.mp4', offset_seconds: 12 }]);
  expect(custom.label).toBe('My camera'); expect(custom.metadata).toBeUndefined();
  const [mismatch] = registeredFeeds([{ match_id: 'cam-1', label: 'My camera', url: getVideoUrl(FEEDS[0]),
    source_key: 'match-1.mp4', source_identity: 'wrong-digest', offset_seconds: 0 }]);
  expect(mismatch.metadata).toBeUndefined(); expect(replayScoreAt(mismatch, 120)).toBeNull();
  expect(readCaptionTrack(trackFor(FEEDS[0]), mismatch)).toBeNull();
});

test('scoreline is bounded to verified source intervals, including the deliberate goal-time gap', () => {
  expect(replayScoreAt(FEEDS[0], 119)).toBeNull(); expect(replayScoreAt(FEEDS[0], 120)).toBe('0–0');
  expect(replayScoreAt(FEEDS[0], 600)).toBeNull(); expect(replayScoreAt(FEEDS[4], 140)).toBe('0–0');
  expect(replayScoreAt(FEEDS[4], 155)).toBeNull(); expect(replayScoreAt(FEEDS[4], 160)).toBe('0–1');
});

test('local search supports natural language, aliases, fuzzy spelling and ambiguous Manchester clubs', () => {
  expect(searchCatalogue(catalogue, 'Where is Arsenal?')[0].team_id).toBe(1);
  expect(searchCatalogue(catalogue, 'Arsnal')[0].team_id).toBe(1);
  expect(searchCatalogue(catalogue, 'Spanish league')[0].competition_code).toBe('PD');
  expect(searchCatalogue(catalogue, 'Manchester').filter((item) => item.kind === 'team')).toHaveLength(2);
});

test('Tables uses cached actual rows, highlights a unique club and makes no request while typing', async () => {
  fetch.mockImplementation((url) => reply(url.includes('/catalog') ? catalogue : standings));
  render(<TablesPage active />);
  await screen.findByRole('table');
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
  fireEvent.change(screen.getByLabelText('Find a league or club'), { target: { value: 'Where is Arsenal?' } });
  expect(fetch).toHaveBeenCalledTimes(3);
  fireEvent.click(screen.getByRole('button', { name: /Arsenal FC Premier League/ }));
  expect(screen.getByRole('row', { name: /1 Arsenal FC/ })).toHaveClass('highlighted-club');
  expect(screen.getByRole('cell', { name: '25' })).toBeInTheDocument();
});

test('ambiguous club submission does not arbitrarily highlight a team', async () => {
  fetch.mockImplementation((url) => reply(url.includes('/catalog') ? catalogue : standings));
  render(<TablesPage active />); await screen.findByRole('table');
  fireEvent.change(screen.getByLabelText('Find a league or club'), { target: { value: 'Manchester' } });
  expect(screen.getByText('More than one club matches. Choose the club you mean.')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Find' }));
  expect(document.querySelector('.highlighted-club')).toBeNull();
});

test('late plain catalogue cannot replace the complete cross-league team catalogue', async () => {
  let finishPlain;
  const complete = { ...catalogue, teams: [...catalogue.teams, { id: 9, name: 'Barcelona FC', aliases: ['Barcelona'], competition_code: 'PD' }] };
  fetch.mockImplementation((url) => url.includes('/catalog') ? url.includes('load_teams=1') ? reply(complete)
    : new Promise((resolve) => { finishPlain = () => resolve(reply({ ...catalogue, teams: [] })); }) : reply(standings));
  render(<TablesPage active />); await screen.findByRole('table');
  fireEvent.change(screen.getByLabelText('Find a league or club'), { target: { value: 'Barcelona' } });
  expect(await screen.findByRole('button', { name: /Barcelona FC Primera Division/ })).toBeInTheDocument();
  await act(async () => finishPlain());
  expect(screen.getByRole('button', { name: /Barcelona FC Primera Division/ })).toBeInTheDocument();
});

test('missing standings key shows setup state without invented table rows', async () => {
  fetch.mockImplementation((url) => reply(url.includes('/catalog') ? { ...catalogue, teams: [], team_catalog_status: 'missing_key' }
    : { status: 'missing_key', message: 'Football provider key is not configured.', setup_url: 'https://www.football-data.org/' }));
  render(<TablesPage active />); await screen.findByText('Connect standings data');
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Provider setup' })).toHaveAttribute('href', 'https://www.football-data.org/');
});

test('cached standings label stale data, preserve missing statistics and provider form without guessing', async () => {
  fetch.mockImplementation((url) => reply(url.includes('/catalog') ? catalogue : { ...standings, stale: true, status: 'rate_limited',
    tables: [{ type: 'TOTAL', rows: [{ ...standings.tables[0].rows[0], goals_for: null, form: 'W,W,D' }] }] }));
  render(<TablesPage active />); await screen.findByRole('table');
  expect(screen.getByText(/Showing cached provider standings/)).toBeInTheDocument();
  expect(screen.getByRole('cell', { name: '—' })).toBeInTheDocument();
  expect(screen.getByRole('cell', { name: 'W,W,D' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Football data provided by football-data.org' })).toHaveAttribute('href', 'https://www.football-data.org/');
});

test('provider failures and rate limits stay honest and retry only on request', async () => {
  fetch.mockImplementation((url) => url.includes('/catalog') ? reply(catalogue) : reply({ status: 'rate_limited', retry_after_seconds: 30 }));
  render(<TablesPage active />); await screen.findByText('Updates temporarily paused');
  expect(screen.getByText('Try again in about 30 seconds.')).toBeInTheDocument();
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
  const calls = fetch.mock.calls.length;
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(calls + 1));
});

const news = { status: 'ok', fetched_at: Date.now() / 1000, attribution: {
  logo_url: 'https://static.guim.co.uk/powered-by.png', url: 'https://www.theguardian.com/' }, articles: [
  { id: 'older', title: 'Fixture headline for the older article', url: 'https://www.theguardian.com/football/older', published_at: '2026-11-07T10:00:00Z', byline: 'Test Reporter', copyright: 'Test copyright', publisher: 'The Guardian' },
  { id: 'newer', title: 'Fixture headline for the latest article', url: 'https://www.theguardian.com/football/newer', published_at: '2026-11-08T10:00:00Z', publisher: 'The Guardian' }] };
test('News renders newest provider articles first in equal cards, with attribution and no fake thumbnails', async () => {
  fetch.mockImplementation(() => reply(news)); render(<NewsPage active />);
  const headline = await screen.findByRole('heading', { name: news.articles[1].title });
  expect(screen.getAllByRole('article')[0]).toContainElement(headline);
  expect(document.querySelector('.news-card--featured')).toBeNull();
  expect(screen.getByText('Test Reporter')).toBeInTheDocument(); expect(screen.getByText('Test copyright')).toBeInTheDocument();
  expect(screen.getByAltText('Powered by The Guardian').closest('a')).toHaveAttribute('href', 'https://www.theguardian.com/');
  expect(document.querySelectorAll('.news-image')).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: 'Transfers' }));
  await waitFor(() => expect(fetch.mock.calls.at(-1)[0]).toContain('topic=transfers'));
  fireEvent.change(screen.getByLabelText('Search football news'), { target: { value: 'Chelsea' } });
  fireEvent.click(screen.getByRole('button', { name: 'Search' }));
  await waitFor(() => expect(fetch.mock.calls.at(-1)[0]).toContain('q=Chelsea'));
});

test('publisher hard expiry purges rendered articles even without connectivity', async () => {
  jest.useFakeTimers(); fetch.mockImplementation(() => reply({ ...news, content_expires_at: Date.now() / 1000 + 1 }));
  render(<NewsPage active />); await act(async () => {});
  expect(screen.getByRole('heading', { name: news.articles[1].title })).toBeInTheDocument();
  act(() => jest.advanceTimersByTime(1001));
  expect(screen.queryByRole('article')).not.toBeInTheDocument();
  expect(screen.getByText(/publisher cache has expired/i)).toBeInTheDocument();
});

test('news deduplicates provider articles and missing keys never become sample stories', async () => {
  fetch.mockImplementationOnce(() => reply({ ...news, stale: true, articles: [news.articles[0], news.articles[0]] }))
    .mockImplementation(() => reply({ status: 'missing_key' }));
  render(<NewsPage active />); await screen.findByRole('article');
  expect(screen.getAllByRole('article')).toHaveLength(1);
  expect(screen.getByText(/cached article details, not a live news update/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: news.articles[0].title })).toHaveAttribute('href', news.articles[0].url);
  fireEvent.click(screen.getByRole('button', { name: 'Champions League' }));
  await screen.findByText('Connect news data'); expect(screen.queryByRole('article')).not.toBeInTheDocument();
});

const trackFor = (feed) => ({ status: 'ready', match_id: feed.cameraId, source_key: feed.sourceKey, source_identity: feed.sourceIdentity,
  timing_accuracy: 'provider_word', provenance: { provider: 'elevenlabs', model: 'scribe_v2', timestamp_method: 'provider_word', machine_generated: true, reviewed: false }, coverage_intervals: [{ start_seconds: 120, end_seconds: 140 }], cues: [
    { id: 'test-1', start_seconds: 120, end_seconds: 125, text: 'Test commentary at source 120.' },
    { id: 'test-2', start_seconds: 130, end_seconds: 135, text: 'Test commentary after the seek.' }] });
function mediaAt(time) {
  const video = document.createElement('video'); video.src = getVideoUrl(FEEDS[0]); video.currentTime = time;
  Object.defineProperty(video, 'readyState', { configurable: true, value: 2 });
  const container = document.createElement('div'); container.append(video); document.body.append(container);
  return { video, container, ref: { current: video } };
}
test('caption tracks reject identity mismatch and deduplicate overlapping actual cues', () => {
  const raw = trackFor(FEEDS[0]);
  expect(readCaptionTrack({ ...raw, source_identity: 'wrong' }, FEEDS[0])).toBeNull();
  const track = readCaptionTrack({ ...raw, cues: [...raw.cues, raw.cues[0], { ...raw.cues[0], id: 'overlap', text: 'Second actual segment.' }] }, FEEDS[0]);
  expect(track.cues).toHaveLength(3);
  expect(captionAt(track, 120.5)).toBe('Test commentary at source 120.\nSecond actual segment.');
  expect(captionAt(track, 125)).toBe('');
});

test('ElevenLabs commentary requires actual provider word provenance and rejects unsupported timing claims', () => {
  const raw = trackFor(FEEDS[0]);
  expect(readCaptionTrack(raw, FEEDS[0])).not.toBeNull();
  expect(readCaptionTrack({ ...raw, provenance: { ...raw.provenance, provider: 'unverified-provider' } }, FEEDS[0])).toBeNull();
  expect(readCaptionTrack({ ...raw, provenance: { ...raw.provenance, model: 'unverified-model' } }, FEEDS[0])).toBeNull();
  expect(readCaptionTrack({ ...raw, timing_accuracy: 'approximate', provenance: { ...raw.provenance, timestamp_method: 'estimated' } }, FEEDS[0])).toBeNull();
});

test('provider speaker labels and audio events preserve independent cues and their actual source boundaries', () => {
  const raw = trackFor(FEEDS[0]);
  const track = readCaptionTrack({ ...raw, cues: [
    { id: 'test-speaker-0', kind: 'speech', speaker_id: 'speaker_0', start_seconds: 120.2, end_seconds: 120.9, text: 'TEST FIXTURE' },
    { id: 'test-speaker-1', kind: 'speech', speaker_id: 'speaker_1', start_seconds: 120.2, end_seconds: 120.9, text: 'TEST FIXTURE' },
    { id: 'test-event', kind: 'audio_event', start_seconds: 120.4, end_seconds: 121, text: '(applause)' },
  ] }, FEEDS[0]);
  expect(track.cues).toHaveLength(3);
  expect(captionAt(track, 120.3)).toBe('Speaker 1: TEST FIXTURE\nSpeaker 2: TEST FIXTURE');
  expect(captionAt(track, 120.5)).toBe('Speaker 1: TEST FIXTURE\nSpeaker 2: TEST FIXTURE\n(applause)');
  expect(captionAt(track, 120.9)).toBe('(applause)');
  expect(captionAt(track, 121)).toBe('');
});

test('Watch retains ElevenLabs caption attribution without an About page', () => {
  render(<App />);
  const captions = screen.getByRole('region', { name: 'Commentary captions' });
  expect(within(captions).getByRole('link', { name: 'ElevenLabs' })).toHaveAttribute('href', 'https://elevenlabs.io/');
  expect(captions).toHaveTextContent(/not human reviewed/i);
  expect(screen.queryByRole('link', { name: 'About' })).not.toBeInTheDocument();
});

test('captions use actual SOURCE seconds, preserve pause and clear immediately on seek/switch', async () => {
  fetch.mockImplementation((url) => reply(trackFor(url.includes('cam-2') ? FEEDS[1] : FEEDS[0])));
  const { video, container, ref } = mediaAt(120.5);
  const { rerender } = render(<CommentaryCaptions feed={{ ...FEEDS[0], offsetSeconds: 120 }} mediaRef={ref} />);
  fireEvent.click(screen.getByRole('button', { name: /Commentary captions Off/ }));
  expect(await screen.findByText('Test commentary at source 120.')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'ElevenLabs' })).toHaveAttribute('href', 'https://elevenlabs.io/');
  expect(screen.getByRole('link', { name: 'ElevenLabs' }).closest('p')).toHaveTextContent(/not human reviewed/i);
  expect(fetch.mock.calls[0][0]).toContain('source_identity=');
  fireEvent.pause(video); expect(screen.getByText('Test commentary at source 120.')).toBeInTheDocument();
  fireEvent.seeking(video); expect(screen.queryByText('Test commentary at source 120.')).not.toBeInTheDocument();
  video.currentTime = 130.5; fireEvent.seeked(video);
  expect(screen.getByText('Test commentary after the seek.')).toBeInTheDocument();
  rerender(<CommentaryCaptions feed={FEEDS[1]} mediaRef={ref} />);
  expect(screen.queryByText('Test commentary after the seek.')).not.toBeInTheDocument();
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  expect(screen.queryByText('Test commentary after the seek.')).not.toBeInTheDocument();
  video.src = getVideoUrl(FEEDS[1]); fireEvent.canPlay(video);
  expect(await screen.findByText('Test commentary after the seek.')).toBeInTheDocument();
  container.remove();
});

test('captions show honest unavailable state in the fixed panel without extra controls', async () => {
  fetch.mockImplementation(() => reply({ status: 'unavailable' }));
  const { ref, container } = mediaAt(120);
  render(<CommentaryCaptions feed={FEEDS[0]} mediaRef={ref} />);
  fireEvent.click(screen.getByRole('button', { name: /Commentary captions Off/ }));
  expect(await screen.findByText(/No commentary track has been generated/)).toBeInTheDocument();
  const captions = screen.getByRole('region', { name: 'Commentary captions' });
  expect(within(captions).queryByRole('combobox')).not.toBeInTheDocument();
  expect(within(captions).getByRole('status')).toHaveTextContent(/No commentary track has been generated/);
  expect(container.querySelector('[aria-live]')).toBeNull();
  container.remove();
});

test('the fixed caption panel ignores obsolete preferences without reading, writing or creating an overlay', async () => {
  localStorage.setItem('matchpulse-captions-size', 'medium');
  localStorage.setItem('matchpulse-captions-layout', 'overlay');
  localStorage.setItem('matchpulse-captions-enabled', 'true');
  fetch.mockImplementation(() => reply(trackFor(FEEDS[0])));
  const { ref, container } = mediaAt(120.5);
  const reads = jest.spyOn(Storage.prototype, 'getItem');
  const writes = jest.spyOn(Storage.prototype, 'setItem');
  try {
    render(<CommentaryCaptions feed={FEEDS[0]} mediaRef={ref} />);
    const text = await screen.findByText('Test commentary at source 120.');
    const captions = screen.getByRole('region', { name: 'Commentary captions' });
    expect(captions).toContainElement(text);
    expect(text).toHaveAttribute('aria-live', 'polite');
    expect(text).toHaveAttribute('aria-atomic', 'true');
    expect(within(captions).queryByRole('combobox')).not.toBeInTheDocument();
    expect(container.querySelector('[aria-live]')).toBeNull();
    fireEvent.click(within(captions).getByRole('button', { name: 'Commentary captions On' }));
    expect(within(captions).getByRole('button', { name: 'Commentary captions Off' })).toHaveAttribute('aria-pressed', 'false');
    expect(writes).toHaveBeenCalledWith('matchpulse-captions-enabled', 'false');
    for (const key of ['matchpulse-captions-size', 'matchpulse-captions-layout']) {
      expect(reads).not.toHaveBeenCalledWith(key);
      expect(writes.mock.calls.filter(([writtenKey]) => writtenKey === key)).toHaveLength(0);
    }
  } finally {
    reads.mockRestore(); writes.mockRestore(); container.remove();
  }
});

test('caption visibility is keyboard operable and retained locally', async () => {
  fetch.mockImplementation(() => reply({ status: 'unavailable' }));
  const { ref, container } = mediaAt(120);
  render(<CommentaryCaptions feed={FEEDS[0]} mediaRef={ref} />);
  screen.getByRole('button', { name: /Commentary captions Off/ }).focus();
  userEvent.keyboard('{Enter}');
  await screen.findByText(/No commentary track has been generated/);
  expect(screen.getByRole('button', { name: /Commentary captions On/ })).toHaveAttribute('aria-pressed', 'true');
  expect(localStorage.getItem('matchpulse-captions-enabled')).toBe('true');
  container.remove();
});

test.each([['default retry', undefined, 30000], ['provider backoff', 60, 60000]])('missing caption track becomes ready without a toggle and respects %s', async (_, backoff, interval) => {
  jest.useFakeTimers(); localStorage.setItem('matchpulse-captions-enabled', 'true');
  fetch.mockImplementationOnce(() => reply({ status: 'unavailable', retry_after_seconds: backoff }))
    .mockImplementation(() => reply(trackFor(FEEDS[0])));
  const { video, ref, container } = mediaAt(120.5);
  render(<CommentaryCaptions feed={FEEDS[0]} mediaRef={ref} />); await act(async () => {});
  expect(screen.getByText(/No commentary track has been generated/)).toBeInTheDocument();
  await act(async () => jest.advanceTimersByTime(interval - 1)); expect(fetch).toHaveBeenCalledTimes(1);
  await act(async () => jest.advanceTimersByTime(1)); expect(fetch).toHaveBeenCalledTimes(2);
  expect(screen.getByText('Test commentary at source 120.')).toBeInTheDocument();
  expect(video.currentTime).toBe(120.5); container.remove();
});

test('actual same-URL media reload clears old captions and coalesces duplicate reload events', async () => {
  jest.useFakeTimers(); localStorage.setItem('matchpulse-captions-enabled', 'true');
  fetch.mockImplementationOnce(() => reply(trackFor(FEEDS[0]))).mockImplementation(() => reply({ ...trackFor(FEEDS[0]),
    cues: [{ id: 'updated-test', start_seconds: 120, end_seconds: 125, text: 'Reloaded actual track fixture.' }] }));
  const { video, ref, container } = mediaAt(120.5);
  render(<CommentaryCaptions feed={FEEDS[0]} mediaRef={ref} />); await act(async () => {});
  expect(screen.getByText('Test commentary at source 120.')).toBeInTheDocument();
  fireEvent.loadStart(video); fireEvent.loadStart(video); fireEvent.loadStart(video);
  expect(screen.queryByText('Test commentary at source 120.')).not.toBeInTheDocument();
  fireEvent.canPlay(video);
  await act(async () => jest.advanceTimersByTime(4999)); expect(fetch).toHaveBeenCalledTimes(1);
  await act(async () => jest.advanceTimersByTime(1)); expect(fetch).toHaveBeenCalledTimes(2);
  expect(screen.getByText('Reloaded actual track fixture.')).toBeInTheDocument(); container.remove();
});

test('absolute source validation expiry clears cues and forces revalidation without renewing a cached relative TTL', async () => {
  jest.useFakeTimers(); localStorage.setItem('matchpulse-captions-enabled', 'true');
  let finishValidation;
  fetch.mockImplementationOnce(() => reply({ ...trackFor(FEEDS[0]), source_identity_valid_until: Date.now() / 1000 + 10,
    source_identity_revalidate_after_seconds: 300 }))
    .mockImplementation(() => new Promise((resolve) => { finishValidation = () => resolve(reply({ ...trackFor(FEEDS[0]),
      source_identity_valid_until: Date.now() / 1000 + 300 })); }));
  const { ref, container } = mediaAt(120.5);
  render(<CommentaryCaptions feed={FEEDS[0]} mediaRef={ref} />); await act(async () => {});
  expect(screen.getByText('Test commentary at source 120.')).toBeInTheDocument();
  await act(async () => jest.advanceTimersByTime(10001)); expect(fetch).toHaveBeenCalledTimes(2);
  expect(screen.queryByText('Test commentary at source 120.')).not.toBeInTheDocument();
  await act(async () => finishValidation());
  expect(screen.getByText('Test commentary at source 120.')).toBeInTheDocument(); container.remove();
});

test('same-URL logical camera and identity changes use an already aligned MainPlayer without waiting for another native event', async () => {
  localStorage.setItem('matchpulse-captions-enabled', 'true');
  const clock = { current: { time: 120.5, playing: false, rate: 1, muted: false, volume: 1 } };
  const ref = { current: null };
  const first = { ...FEEDS[0], url: getVideoUrl(FEEDS[0]) };
  const second = { ...FEEDS[1], url: first.url };
  const updated = { ...second, sourceIdentity: 'updated-test-identity' };
  fetch.mockImplementation((url) => {
    const feed = url.includes('updated-test-identity') ? updated : url.includes('cam-2') ? second : first;
    return reply({ ...trackFor(feed), cues: [{ id: feed.sourceIdentity, start_seconds: 120, end_seconds: 125,
      text: `Aligned fixture for ${feed.cameraId} ${feed.sourceIdentity}.` }] });
  });
  const view = (feed) => <><MainPlayer feed={feed} clock={clock} mediaRef={ref} /><CommentaryCaptions feed={feed} mediaRef={ref} /></>;
  const { rerender } = render(view(first)); const video = ref.current;
  Object.defineProperty(video, 'readyState', { configurable: true, value: 2 });
  Object.defineProperty(video, 'duration', { configurable: true, value: 1000 });
  fireEvent.loadedMetadata(video); fireEvent.canPlay(video);
  await screen.findByText(`Aligned fixture for cam-1 ${first.sourceIdentity}.`);
  rerender(view(second));
  expect(ref.current).toBe(video);
  expect(screen.queryByText(`Aligned fixture for cam-1 ${first.sourceIdentity}.`)).not.toBeInTheDocument();
  await screen.findByText(`Aligned fixture for cam-2 ${second.sourceIdentity}.`);
  rerender(view(updated));
  expect(screen.queryByText(`Aligned fixture for cam-2 ${second.sourceIdentity}.`)).not.toBeInTheDocument();
  await screen.findByText('Aligned fixture for cam-2 updated-test-identity.');
  expect(video.currentTime).toBe(120.5);
});

test.each(['manual', 'automatic'])('%s selection changes caption tracks at the actual offset source time without replacing pooled players', async (kind) => {
  localStorage.setItem('matchpulse-captions-enabled', 'true');
  const { container } = render(<App />);
  const socket = global.FakeWebSocket.instances.at(-1);
  const baseSession = { type: 'session', protocol_version: 1, session_id: 'caption-integration', sequence: 1,
    state: 'running', mode: 'features_mock', model_version: 'test-mock', server_time: Date.now() / 1000,
    playback: { position: 0, playing: true, rate: 1, clock_epoch: 0 }, current_match_id: 'cam-1', auto_enabled: true, manual_hold_until: 0,
    captions: { available: true, enabled: true, status: 'connecting', model: 'scribe_v2_realtime', generation: 1, match_id: 'cam-1', clock_epoch: 0 },
    matches: FEEDS.map((feed) => ({ match_id: feed.cameraId, label: feed.label, url: getVideoUrl(feed), source_key: feed.sourceKey,
      source_identity: feed.sourceIdentity, offset_seconds: feed.id === 2 ? 200 : 120, healthy: true })) };
  act(() => { socket.open(); socket.receive(baseSession); });
  const liveEvent = (feed, sequence, generation, anchor) => ({ type: 'caption', session_id: 'caption-integration', sequence,
    generation, clock_epoch: 0, match_id: feed.cameraId, source_key: feed.sourceKey, source_identity: feed.sourceIdentity,
    provider: 'elevenlabs', model: 'scribe_v2_realtime', state: 'partial', source_anchor_seconds: anchor,
    text: `Actual live fixture for ${feed.cameraId}.` });
  const video = screen.getByLabelText(`${FEEDS[0].label} video`);
  const nodes = [...container.querySelectorAll('video')];
  const destination = container.querySelector(`.tile[data-feed-id="${FEEDS[1].id}"] video`);
  expect(destination).not.toHaveAttribute('src');
  expect(nodes).toHaveLength(FEEDS.length);
  expect(destination).not.toBe(video);
  Object.defineProperty(video, 'readyState', { configurable: true, value: 2 });
  Object.defineProperty(video, 'duration', { configurable: true, value: 1000 });
  fireEvent.loadedMetadata(video); fireEvent.canPlay(video);
  expect(video.currentTime).toBe(120);
  const destinationSource = destination.getAttribute('src');
  expect(destinationSource).toBe(getVideoUrl(FEEDS[1]));
  if (kind === 'automatic') {
    // Auto can promote only an already decoded, aligned native pool player.
    Object.defineProperty(destination, 'readyState', { configurable: true, value: 2 });
    Object.defineProperty(destination, 'duration', { configurable: true, value: 1000 });
    fireEvent.loadedMetadata(destination); fireEvent.canPlay(destination);
    expect(destination.currentTime).toBe(200);
  }
  act(() => socket.receive(liveEvent(FEEDS[0], 2, 1, 120)));
  expect(screen.getByText('Actual live fixture for cam-1.')).toBeInTheDocument();
  if (kind === 'manual') fireEvent.click(screen.getByRole('button', { name: FEEDS[1].label }));
  else act(() => socket.receive({ type: 'recommendation', protocol_version: 1, session_id: 'caption-integration', sequence: 3,
    model_version: 'test-mock', score_kind: 'mock', clock_epoch: 0, selected_match_id: 'cam-2', playback_time: 0, observation_end: 0,
    emitted_at: Date.now() / 1000, valid_until: Date.now() / 1000 + 3, scores: { 'cam-1': .1, 'cam-2': .9 } }));
  expect(screen.queryByText('Actual live fixture for cam-1.')).not.toBeInTheDocument();
  expect(screen.getByLabelText(`${FEEDS[1].label} video`)).toBe(destination);
  expect(container.querySelector('.tile--main video')).toBe(destination);
  expect(video.closest('.tile')).toHaveClass('tile--preview');
  expect(destination.getAttribute('src')).toBe(destinationSource);
  Object.defineProperty(destination, 'readyState', { configurable: true, value: 2 });
  Object.defineProperty(destination, 'duration', { configurable: true, value: 1000 });
  fireEvent.loadedMetadata(destination); fireEvent.canPlay(destination);
  expect(destination.currentTime).toBe(200);
  expect([...container.querySelectorAll('video')]).toHaveLength(nodes.length);
  [...container.querySelectorAll('video')].forEach((node, index) => expect(node).toBe(nodes[index]));
  act(() => { socket.receive({ ...baseSession, sequence: 4, current_match_id: 'cam-2', captions: { ...baseSession.captions, generation: 2, match_id: 'cam-2' } });
    socket.receive(liveEvent(FEEDS[1], 5, 2, 200)); });
  expect(screen.getByText('Actual live fixture for cam-2.')).toBeInTheDocument();
  expect(fetch).not.toHaveBeenCalled();
  if (kind === 'automatic') expect(socket.sent.filter((message) => message.type === 'ack')).toHaveLength(1);
});

test('all three pages keep all seven pooled match players mounted', async () => {
  fetch.mockImplementation((url) => reply(url.includes('/catalog') ? catalogue : url.includes('/standings') ? standings : { status: 'missing_key' }));
  const { container } = render(<App />);
  const videos = [...container.querySelectorAll('video')];
  const focused = screen.getByLabelText(`${FEEDS[0].label} video`);
  const navigation = screen.getByRole('navigation', { name: 'Main navigation' });
  for (const name of ['Tables', 'News', 'Watch']) {
    await act(async () => fireEvent.click(within(navigation).getByRole('link', { name })));
    const current = [...container.querySelectorAll('video')];
    expect(current).toHaveLength(videos.length);
    current.forEach((node, index) => expect(node).toBe(videos[index]));
    expect(container.querySelector('.tile--main video')).toBe(focused);
  }
  expect(videos).toHaveLength(FEEDS.length);
});
