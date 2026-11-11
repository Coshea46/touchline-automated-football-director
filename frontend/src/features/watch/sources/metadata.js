export function replayScoreAt(feed, sourceTime) {
  if (!Number.isFinite(sourceTime)) return null;
  const interval = feed.metadata?.replay?.score_intervals?.find((item) => item.verified === true
    && Number.isFinite(item.start_seconds) && Number.isFinite(item.end_seconds)
    && item.start_seconds <= sourceTime && sourceTime < item.end_seconds
    && Number.isInteger(item.home) && item.home >= 0 && Number.isInteger(item.away) && item.away >= 0);
  return interval ? `${interval.home}–${interval.away}` : null;
}
