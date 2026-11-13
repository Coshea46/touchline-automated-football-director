import startupFailure from './startupFailure';

const replay = (state) => ({ visitor: { phase: 'ready' }, sessionState: state });
test('worker connection failure is distinguished from cancellation and budget denial', () => {
  expect(startupFailure(replay({ state: 'error', error_code: 'inference_unavailable' }))).toMatch(/connect to its AI workers/);
  expect(startupFailure(replay({ state: 'disabled', ai_reason: 'budget_exhausted' }))).toMatch(/spending allowance/);
  expect(startupFailure(replay({ state: 'starting', ai_reason: 'active' }))).toBe('');
});
