import { render, screen } from '@testing-library/react';
import CommentaryCaptions from './CommentaryCaptions';
import { FEEDS, getVideoUrl } from '../watch/sources/feeds';
import { LIVE_CAPTION_MODEL, liveCaptionStatus, readLiveCaption } from './liveCaptions';

const feed = FEEDS[0];
const live = { available: true, enabled: true, status: 'listening', state: 'committed',
  match_id: feed.cameraId, source_key: feed.sourceKey, source_identity: feed.sourceIdentity,
  text: 'Test focused-source caption.' };
function media() {
  const video = document.createElement('video');
  video.src = getVideoUrl(feed); video.currentTime = 120;
  Object.defineProperty(video, 'readyState', { value: 2 });
  return { current: video };
}

test('offline captions describe automatic connection recovery and preserve requested On preference', () => {
  render(<CommentaryCaptions feed={feed} mediaRef={media()} live={live} enabled connected={false} />);
  expect(screen.getByRole('button', { name: 'Commentary captions On' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByText(/Reconnecting automatically; no live captions are being received/)).toBeInTheDocument();
  expect(screen.queryByText(live.text)).not.toBeInTheDocument();
});

test.each(['budget_deadline', 'budget_exhausted', 'audio_limit'])('known caption %s remains visible when the backend disconnects', (reason) => {
  expect(liveCaptionStatus({ status: 'stopped', reason }, false)).toMatch(/session allowance has been reached/);
});

test.each([['missing_key', /operator needs to connect ElevenLabs/], ['auth_error', /Speech to Text access/],
  ['quota_exceeded', /temporarily limited by the provider/]])('known %s is not misreported as only a connection failure', (reason, message) => {
  expect(liveCaptionStatus({ status: 'unavailable', reason }, false)).toMatch(message);
});

test('an authoritative closed caption session clears old text and explains its end', () => {
  render(<CommentaryCaptions feed={feed} mediaRef={media()} enabled connected
    live={{ ...live, status: 'stopped', reason: 'session_closed' }} />);
  expect(screen.queryByText(live.text)).not.toBeInTheDocument();
  expect(screen.getByText(/replay session has ended.*new replay session/)).toBeInTheDocument();
});

test('ML monitoring expiry alone does not hide an independently active caption stream', () => {
  render(<CommentaryCaptions feed={feed} mediaRef={media()} enabled connected live={live} replayState="stopped" />);
  expect(screen.getByText(live.text)).toBeInTheDocument();
  expect(liveCaptionStatus(live, true, true, 'stopped')).toMatch(/Listening to the focused match/);
});

test('a forty-minute server allowance preserves valid captions beyond the former smoke window', () => {
  const session = { session_id: 'long-caption-session', sequence: 1, playback: { clock_epoch: 4 },
    captions: { enabled: true, generation: 7, match_id: feed.cameraId, clock_epoch: 4,
      max_audio_seconds: 2400, max_cost_usd: .30, deadline: 3400 } };
  const message = { type: 'caption', session_id: session.session_id, sequence: 2, generation: 7,
    clock_epoch: 4, match_id: feed.cameraId, source_key: feed.sourceKey, source_identity: feed.sourceIdentity,
    provider: 'elevenlabs', model: LIVE_CAPTION_MODEL, state: 'committed', text: 'Test later-session caption.',
    source_anchor_seconds: 1800, words: [{ text: 'Test', start_seconds: 1800, end_seconds: 1800.5 }] };
  const playback = { playing: true, available: true, stalled: false, sourceTime: 1801 };
  expect(readLiveCaption(message, session, feed, playback)).toMatchObject({ text: message.text, generation: 7 });
  expect(readLiveCaption({ ...message, generation: 6 }, session, feed, playback)).toBeNull();
  expect(readLiveCaption({ ...message, source_anchor_seconds: 1802 }, session, feed, playback)).toBeNull();
});
