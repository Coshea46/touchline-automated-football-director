import { act, fireEvent, render, screen } from '@testing-library/react';
import NewsPage from '../features/news/NewsPage';
import TablesPage from '../features/tables/TablesPage';
import { API_RECOVERY_EVENT, API_REQUEST_TIMEOUT_MS, apiGet, clearApiCache, isPlatformConnectionError } from '../shared/api/platformApi';

const reply = (body, ok = true) => Promise.resolve({ ok, json: () => Promise.resolve(body) });
const news = { status: 'ok', fetched_at: Date.now() / 1000, articles: [{ id: 'test-recovery',
  title: 'Test provider headline after recovery', url: 'https://www.theguardian.com/football/test-recovery',
  published_at: '2026-11-08T12:00:00Z', publisher: 'The Guardian' }] };
const flush = async () => { await act(async () => {}); };

beforeEach(() => { clearApiCache(); global.fetch = jest.fn(); });
afterEach(() => { clearApiCache(); delete global.fetch; jest.useRealTimers(); });

test('network failures are actionable, sanitized and never cached as provider failures', async () => {
  fetch.mockRejectedValueOnce(new TypeError('Failed to fetch https://private.example/?token=secret'));
  const error = await apiGet('/api/news').catch((value) => value);
  expect(isPlatformConnectionError(error)).toBe(true);
  expect(error.message).toMatch(/data service could not be reached.*Retrying automatically.*backend/);
  expect(error.message).not.toMatch(/private|token|secret|Failed to fetch/);
  fetch.mockImplementationOnce(() => reply(news));
  expect(await apiGet('/api/news')).toBe(news);
  expect(fetch).toHaveBeenCalledTimes(2);
});

test.each(['missing_key', 'rate_limited', 'provider_error'])('HTTP provider %s remains distinct and is not automatically retried', async (status) => {
  jest.useFakeTimers();
  const body = { status, message: 'Test provider response.', retry_after_seconds: 90 };
  fetch.mockImplementation(() => reply(body, false));
  render(<NewsPage active />);
  await flush();
  expect(screen.getByText('Test provider response.')).toBeInTheDocument();
  act(() => { jest.advanceTimersByTime(120000); window.dispatchEvent(new Event(API_RECOVERY_EVENT)); });
  await flush();
  expect(fetch).toHaveBeenCalledTimes(1);
});

test('a hung HTTP request is bounded and classified as a connection failure', async () => {
  jest.useFakeTimers();
  fetch.mockImplementation((url, { signal }) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
  }));
  const pending = apiGet('/api/news').catch((error) => error);
  await flush();
  act(() => jest.advanceTimersByTime(API_REQUEST_TIMEOUT_MS));
  expect(isPlatformConnectionError(await pending)).toBe(true);
});

test('News recovers on a bounded retry when the backend returns without a page reload', async () => {
  jest.useFakeTimers();
  fetch.mockRejectedValueOnce(new TypeError('Failed to fetch')).mockImplementation(() => reply(news));
  render(<NewsPage active />);
  await flush();
  expect(screen.getByText(/data service could not be reached/)).toBeInTheDocument();
  expect(screen.queryByRole('article')).not.toBeInTheDocument();
  act(() => jest.advanceTimersByTime(4999)); await flush();
  expect(fetch).toHaveBeenCalledTimes(1);
  act(() => jest.advanceTimersByTime(1)); await flush();
  expect(screen.getByRole('heading', { name: news.articles[0].title })).toBeInTheDocument();
  expect(fetch).toHaveBeenCalledTimes(2);
});

test('socket recovery refreshes a failed Tables view and its catalogue with no user retry', async () => {
  jest.useFakeTimers();
  fetch.mockRejectedValue(new TypeError('Failed to fetch'));
  render(<TablesPage active />);
  await flush();
  expect(screen.getByText(/data service could not be reached/)).toBeInTheDocument();
  const calls = fetch.mock.calls.length;
  fetch.mockImplementation((url) => reply(url.includes('/catalog')
    ? { status: 'ok', competitions: [{ code: 'PL', name: 'Premier League' }], teams: [] }
    : { status: 'ok', competition: { code: 'PL', name: 'Premier League' }, tables: [{ type: 'TOTAL', rows: [{
      position: 1, team: { id: 1, name: 'Test Club' }, played: 1, won: 1, drawn: 0, lost: 0,
      goals_for: 1, goals_against: 0, goal_difference: 1, points: 3,
    }] }] }));
  act(() => window.dispatchEvent(new Event(API_RECOVERY_EVENT))); await flush();
  expect(screen.getByRole('table')).toBeInTheDocument();
  expect(screen.getByText('Test Club')).toBeInTheDocument();
  expect(fetch.mock.calls.length).toBe(calls + 3);
});

test('hidden pages cancel connection retries and a manual retry still works immediately', async () => {
  jest.useFakeTimers();
  fetch.mockRejectedValue(new TypeError('Failed to fetch'));
  const { rerender } = render(<NewsPage active />); await flush();
  const calls = fetch.mock.calls.length;
  rerender(<NewsPage active={false} />);
  act(() => { jest.advanceTimersByTime(60000); window.dispatchEvent(new Event('online')); }); await flush();
  expect(fetch).toHaveBeenCalledTimes(calls);
  rerender(<NewsPage active />); await flush();
  fetch.mockImplementation(() => reply(news));
  fireEvent.click(screen.getByRole('button', { name: 'Try again' })); await flush();
  expect(screen.getByRole('heading', { name: news.articles[0].title })).toBeInTheDocument();
});
