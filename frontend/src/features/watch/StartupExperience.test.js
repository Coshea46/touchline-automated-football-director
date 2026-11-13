import { act, fireEvent, render, screen } from '@testing-library/react';
import StartupExperience from './StartupExperience';

beforeEach(() => jest.useFakeTimers());
afterEach(() => jest.useRealTimers());
test('tutorial completion waits for real readiness, then fades to the app', () => {
  const enter = jest.fn(), cancel = jest.fn();
  const props = { onEnter: enter, onCancel: cancel, onRetry: jest.fn() };
  const view = render(<StartupExperience {...props} ready={false} />);
  fireEvent.ended(screen.getByLabelText('Touchline tutorial'));
  expect(screen.getByRole('heading', { name: 'Setting things up' })).toBeVisible();
  act(() => jest.advanceTimersByTime(10000));
  expect(enter).not.toHaveBeenCalled();
  view.rerender(<StartupExperience {...props} ready />);
  act(() => jest.advanceTimersByTime(350));
  expect(enter).toHaveBeenCalledTimes(1);
  expect(cancel).not.toHaveBeenCalled();
});
test('skip does not bypass readiness and setup has a finite timeout', () => {
  const enter = jest.fn(), cancel = jest.fn();
  render(<StartupExperience ready={false} onEnter={enter} onCancel={cancel} />);
  fireEvent.click(screen.getByText('Skip tutorial'));
  expect(enter).not.toHaveBeenCalled();
  act(() => jest.advanceTimersByTime(120000));
  expect(cancel).toHaveBeenCalledTimes(1);
  expect(cancel).toHaveBeenCalledWith(expect.stringContaining('startup took too long'), true);
  expect(enter).not.toHaveBeenCalled();
});

test.each(['ended', 'skip'])('automatic failure preserves the tutorial until %s without stealing control focus', (finish) => {
  const enter = jest.fn(), cancel = jest.fn();
  const props = { ready: false, cancelled: false, onEnter: enter, onCancel: cancel, onRetry: jest.fn(), onManual: jest.fn(),
    notice: 'The AI spending allowance has been reached.', retryAllowed: false };
  const view = render(<StartupExperience {...props} />);
  const tutorial = screen.getByLabelText('Touchline tutorial');
  const skip = screen.getByRole('button', { name: 'Skip tutorial' });
  skip.focus();
  view.rerender(<StartupExperience {...props} cancelled deferFailure />);
  expect(screen.getByLabelText('Touchline tutorial')).toBe(tutorial);
  expect(screen.getByRole('heading', { name: 'Welcome to Touchline' })).toBeVisible();
  expect(document.activeElement).toBe(skip);
  act(() => jest.advanceTimersByTime(120000));
  expect(cancel).not.toHaveBeenCalled();
  expect(screen.getByLabelText('Touchline tutorial')).toBe(tutorial);
  if (finish === 'ended') fireEvent.ended(tutorial);
  else fireEvent.click(skip);
  expect(screen.getByRole('heading', { name: 'Setup paused' })).toBeVisible();
  expect(screen.getByText(props.notice)).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Continue without AI' })).toBeVisible();
  expect(enter).not.toHaveBeenCalled();
});
test('cancelled setup cannot transition even if a late ready event arrives', () => {
  const enter = jest.fn();
  render(<StartupExperience ready cancelled onEnter={enter} onCancel={jest.fn()} onRetry={jest.fn()} />);
  act(() => jest.advanceTimersByTime(1000));
  expect(screen.getByRole('heading', { name: 'Setup paused' })).toBeVisible();
  expect(enter).not.toHaveBeenCalled();
});
