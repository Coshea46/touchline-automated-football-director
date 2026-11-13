import { act, fireEvent, render, screen } from '@testing-library/react';
import StartupExperience from './StartupExperience';

beforeEach(() => jest.useFakeTimers());
afterEach(() => { jest.useRealTimers(); jest.restoreAllMocks(); });

test('tries narration with sound, falls back to muted autoplay, and enables sound on a click', async () => {
  const play = jest.spyOn(HTMLMediaElement.prototype, 'play')
    .mockRejectedValueOnce(new DOMException('Autoplay blocked', 'NotAllowedError'))
    .mockResolvedValue();
  await act(async () => render(<StartupExperience ready={false} onEnter={jest.fn()} onCancel={jest.fn()} />));
  const player = screen.getByLabelText('Touchline tutorial');
  expect(play).toHaveBeenCalledTimes(2);
  expect(player.muted).toBe(true);
  expect(screen.getByText(/recordings used to demonstrate Touchline/)).toBeVisible();
  act(() => { player.volume = 0; });
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Turn sound on' })));
  expect(player.muted).toBe(false);
  expect(player.volume).toBe(1);
  expect(play).toHaveBeenCalledTimes(3);
  expect(screen.queryByRole('button', { name: 'Turn sound on' })).not.toBeInTheDocument();
});

test('offers explicit playback when both autoplay attempts are blocked', async () => {
  jest.spyOn(HTMLMediaElement.prototype, 'play').mockRejectedValue(new DOMException('Autoplay blocked', 'NotAllowedError'));
  await act(async () => render(<StartupExperience ready={false} onEnter={jest.fn()} onCancel={jest.fn()} />));
  expect(screen.getByRole('button', { name: 'Play tutorial with sound' })).toBeVisible();
  expect(screen.getByLabelText('Touchline tutorial')).toBeVisible();
});

test('native volume controls keep the sound button in sync', async () => {
  await act(async () => render(<StartupExperience ready={false} onEnter={jest.fn()} onCancel={jest.fn()} />));
  const player = screen.getByLabelText('Touchline tutorial');
  expect(player.muted).toBe(false);
  act(() => { player.muted = true; fireEvent.volumeChange(player); });
  expect(screen.getByRole('button', { name: 'Turn sound on' })).toBeVisible();
  act(() => { player.muted = false; fireEvent.volumeChange(player); });
  expect(screen.queryByRole('button', { name: 'Turn sound on' })).not.toBeInTheDocument();
});
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
