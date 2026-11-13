import { ageLabel, preferredMode, readDirector, replayTime } from './director';
import { FEEDS } from '../sources/feeds';

test('director display accepts only known modes and registered finite relative scores', () => {
  expect(readDirector({ mode: 'invented' }, FEEDS)).toBeNull();
  const value = readDirector({ mode: 'balanced', latest_scores: { 'cam-1': 0, 'cam-2': .9, 'cam-3': NaN, unknown: .8 },
    config: { cooldown_seconds: 12, staying_bonus: .2, confirmations_required: 2 } }, FEEDS);
  expect(value.latest_scores).toEqual({ 'cam-1': 0, 'cam-2': .9 });
  expect(value.config.cooldown_seconds).toBe(12);
  expect(readDirector({ mode: 'balanced', config: { cooldown_seconds: -1 } }, FEEDS).config).toBeNull();
});

test('selection history is bounded to factual registered sources and valid event kinds', () => {
  const event = { id: 1, from_match_id: 'cam-1', to_match_id: 'cam-2', selected_at: 1000,
    playback_time: 12, kind: 'automatic' };
  const value = readDirector({ mode: 'balanced', history: [event, { ...event, id: 2, kind: 'goal' },
    { ...event, id: 3, to_match_id: 'unknown' }] }, FEEDS);
  expect(value.history).toEqual([event]);
  expect(readDirector({ mode: 'balanced', history: Array.from({ length: 120 }, (_, id) => ({ ...event, id })) }, FEEDS).history).toHaveLength(100);
});

test('display times and stored preferences do not invent settings or match metadata', () => {
  localStorage.setItem('matchpulse-director-mode', 'bad');
  expect(preferredMode()).toBe('all_action');
  localStorage.setItem('matchpulse-director-mode', 'balanced');
  expect(preferredMode()).toBe('balanced');
  localStorage.setItem('matchpulse-director-mode', 'all_action');
  expect(preferredMode()).toBe('all_action');
  expect(replayTime(125.5)).toBe('2:05');
  expect(ageLabel(null, 1000)).toBe('Not yet');
  expect(ageLabel(982, 1000)).toBe('18s ago');
  localStorage.clear();
});
