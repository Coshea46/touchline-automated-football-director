"""Persistent logical match workers, shared bounded detector batches and compact scores."""

import math
import sys
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field

from touchline.ml_inference.streaming.contract import MAX_WORKER_RATE, validate_control, validate_session


@dataclass
class _Match:
    source: dict = field(repr=False)
    features: object = field(repr=False)
    next_end: int
    cursor: object = field(default=None, repr=False)
    future: object = field(default=None, repr=False)
    opening: bool = True
    samples: deque = field(default_factory=deque, repr=False)
    processed: int = 0
    scheduled: int = 0
    visual_future: object = field(default=None, repr=False)
    visual_count: int = 0
    visual_invalidated: bool = False
    feature_stats: dict = field(default_factory=dict, repr=False)
    last_error_type: str | None = None
    last_error_phase: str | None = None
    sequence: int = 0
    status: str = 'starting'
    failures: int = 0
    retry_at: float = 0
    last_decode_at: float = 0
    last_read_permit: float | None = None
    reader_seconds: float = 0
    reader_diagnostics: dict = field(default_factory=dict, repr=False)


def run_replay_session(spec, detector, control, publish, *, scorer,
                       detector_config=None, runtime=None, cursor_factory=None,
                       feature_factory=None, state_metrics=None):
    """One bounded invocation owns its decoders and model for the active session.

    The control clock is a strict permission watermark from actual viewer
    reports. No extrapolation is used. Reader queues have at most32 RGB samples;
    observations have75 entries and no RGB. All source failures are sanitized.
    """
    from touchline.ml_inference.features.incremental import IncrementalMatchFeatures
    from touchline.ml_inference.features.media import MediaCursor

    validate_session(spec)
    cursor_factory = cursor_factory or MediaCursor
    feature_factory = feature_factory or IncrementalMatchFeatures
    audio_contract = getattr(scorer, 'audio_contract', 'legacy-gru')
    contract_options = {'audio_contract': audio_contract} if audio_contract == 'sampleclock-v3' else {}
    session_id = spec['session_id']
    started = time.monotonic()
    duration = float(spec.get('max_duration_seconds', 300))
    score_kind = 'model' if spec['mode'] == 'real' else 'mock'
    epoch, contexts = None, {}
    worker_sequence = 0
    scores = {}
    metrics = {'detector_seconds': 0.0, 'detector_frames': 0, 'detector_batches': 0,
               'window_assembly_seconds': 0.0, 'classifier_seconds': 0.0,
               'publication_seconds': 0.0, 'deadline_misses': 0, 'max_queue_samples': 0,
               'windows': 0, 'decoder_failures': 0, 'visual_append_seconds': 0.0,
               'visual_wait_seconds': 0.0, 'visual_jobs': 0, 'visual_failures': 0,
               'max_visual_pending': 0, 'source_failure_types': {},
               'window_assembly_task_seconds': 0.0, 'max_assembly_pending': 0,
               'window_assembly_failures': 0}
    availability = {}
    last_publish, first_pending = 0.0, None
    last_progress_at, previous_permit = started, None
    initialization_started, startup_complete, has_progress = started, False, False
    startup_waiting = False
    readers = ThreadPoolExecutor(max_workers=len(spec['matches']), thread_name_prefix='replay-source')
    visual = ThreadPoolExecutor(max_workers=min(4, len(spec['matches'])), thread_name_prefix='replay-visual')

    def snapshot(state, reason=None):
        nonlocal worker_sequence, last_publish
        worker_sequence += 1
        now = time.time()
        try:
            import resource

            peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            peak_rss_bytes = int(peak_rss if sys.platform == 'darwin' else peak_rss * 1024)
        except (ImportError, OSError):
            peak_rss_bytes = None
        payload = {'session_id': session_id, 'model_version': spec['model_version'],
                   'clock_epoch': epoch or 0, 'worker_sequence': worker_sequence,
                   'state': state, 'reason': reason, 'score_kind': score_kind,
                   'produced_at': now, 'scores': list(scores.values()),
                   'health': {key: {'status': ctx.status,
                        'next_sample_index': ctx.processed,
                        'scheduled_sample_index': ctx.scheduled,
                        'visual_pending_samples': ctx.visual_count,
                        'last_error_type': ctx.last_error_type,
                        'last_error_phase': ctx.last_error_phase,
                        'queue_samples': len(ctx.samples),
                        'metadata': dict(ctx.cursor.metadata) if ctx.cursor else None}
                        for key, ctx in contexts.items()},
                   'metrics': {**metrics, 'elapsed_seconds': time.monotonic() - started,
                        'peak_rss_bytes': peak_rss_bytes,
                        'estimated_compute_usd': (time.monotonic() - started) * MAX_WORKER_RATE,
                        'runtime': runtime or {},
                        'state_io': state_metrics() if state_metrics else {},
                        'streams': {key: {'features': dict(ctx.feature_stats),
                            'reader_seconds': ctx.reader_seconds,
                            'reader': ctx.cursor.stats if ctx.cursor else dict(ctx.reader_diagnostics)}
                            for key, ctx in contexts.items()}}}
        before = time.perf_counter()
        publish(payload)
        metrics['publication_seconds'] += time.perf_counter() - before
        last_publish = time.monotonic()
        return payload

    def retire(ctx):
        opening, cursor = ctx.opening, ctx.cursor
        if ctx.future is not None and not ctx.future.done():
            # Close on that reader's own completion, never concurrently inside
            # an FFmpeg decode. HTTP read timeouts bound this wait.
            ctx.future.cancel()
            def close_after(future):
                # Cleanup must not log a decoder exception containing a URL.
                with suppress(Exception):
                    result = future.result()
                    owned = result if opening else cursor
                    if owned is not None:
                        owned.close()
            ctx.future.add_done_callback(close_after)
        else:
            if ctx.future is not None and opening:
                with suppress(Exception):
                    cursor = ctx.future.result()
            if cursor is not None:
                cursor.close()

    def pull(ctx, permit):
        before = time.perf_counter()
        try:
            return ctx.cursor.pull(permit, 16)
        finally:
            ctx.reader_seconds += time.perf_counter() - before

    def open_cursor(source, offset, base_index, original_permit):
        cursor = cursor_factory(source, offset_seconds=offset, start_index=base_index, **contract_options)
        try:
            # A seek rounds the first full window up to the two-second grid.
            # Never decode a future base just to release its startup buffer.
            if base_index / 15 <= original_permit + 1e-8:
                cursor.prime(original_permit)
            return cursor
        except Exception:
            with suppress(Exception):
                cursor.close()
            raise

    def startup_ready():
        return bool(contexts and any(ctx.cursor is not None for ctx in contexts.values())
                    and all(ctx.status == 'healthy' or ctx.status == 'eof'
                            or (ctx.status == 'error' and ctx.failures > 2)
                            for ctx in contexts.values()))

    def discard_visual(ctx):
        # Epoch replacement and shutdown never reuse this feature object. An
        # already-running CPU job may finish against its retired object only.
        if ctx.visual_future is not None:
            ctx.visual_future.cancel()
        ctx.visual_future, ctx.visual_count = None, 0

    def initialize(clock):
        nonlocal contexts, epoch, scores, first_pending, previous_permit, last_progress_at
        nonlocal initialization_started, startup_complete, has_progress
        for ctx in contexts.values():
            retire(ctx)
            discard_visual(ctx)
        epoch = clock.get('clock_epoch', 0)
        base_index = math.ceil(float(clock['position']) / 2 - 1e-8) * 30
        contexts = {}
        scores = {}
        availability.clear()
        first_pending, previous_permit = None, clock['position']
        last_progress_at = time.monotonic()
        initialization_started, startup_complete, has_progress = time.monotonic(), False, False
        metrics.pop('startup_ready_seconds', None)
        metrics.pop('startup_ready_epoch', None)
        source_updates = {m['match_id']: m for m in clock.get('sources', [])}
        for registered in spec['matches']:
            match = {**registered, **source_updates.get(registered['match_id'], {})}
            key = match['match_id']
            features = feature_factory(key, detector, detector_config=detector_config,
                source_epoch=f'{epoch}:0', timeline_origin_seconds=float(match.get('offset_seconds', 0)),
                **contract_options)
            ctx = _Match(match, features, base_index + 75, processed=base_index,
                         scheduled=base_index, feature_stats=dict(features.stats))
            ctx.future = readers.submit(open_cursor, match['source_url'],
                float(match.get('offset_seconds', 0)), base_index, clock['position'])
            contexts[key] = ctx

    def fail_source(ctx, error_type='RuntimeError', phase='reader'):
        # A pending decoder still owns its FFmpeg objects. Retire closes it on
        # that future's completion, and no stale rows cross the recovery epoch.
        if ctx.cursor is not None:
            with suppress(Exception):
                ctx.reader_diagnostics = dict(ctx.cursor.stats)
        retire(ctx)
        ctx.cursor, ctx.future, ctx.opening = None, None, False
        ctx.status, ctx.failures = 'error', ctx.failures + 1
        ctx.retry_at = time.monotonic() + min(4, ctx.failures)
        ctx.samples.clear()
        ctx.visual_invalidated = True
        if ctx.visual_future is not None:
            ctx.visual_future.cancel()
        ctx.last_error_type, ctx.last_error_phase = error_type, phase
        scores.pop(ctx.source['match_id'], None)
        metrics['decoder_failures'] += 1
        failure = f'{phase}:{error_type}'
        metrics['source_failure_types'][failure] = metrics['source_failure_types'].get(failure, 0) + 1

    def append_visual(ctx, rows):
        before = time.perf_counter()
        ctx.features.append_batch(
            [sample.sample_index for _, sample, _ in rows],
            [sample.rgb for _, sample, _ in rows],
            [sample.source_timestamp for _, sample, _ in rows],
            detections=[raw for _, _, raw in rows])
        return time.perf_counter() - before

    def assemble_visual(ctx, end_index, audio):
        before = time.perf_counter()
        features = ctx.features.assemble(end_index, audio)
        return features, time.perf_counter() - before

    def harvest_visual(ctx, *, wait=False):
        future = ctx.visual_future
        if future is None:
            return not ctx.visual_invalidated
        if not wait and not future.done():
            return False
        count = ctx.visual_count
        ctx.visual_future, ctx.visual_count = None, 0
        before = time.perf_counter()
        try:
            elapsed = future.result()
        except Exception as exc:  # noqa: BLE001 -- isolate a failed match's CPU measurements.
            if not ctx.visual_invalidated:
                metrics['visual_failures'] += 1
                fail_source(ctx, type(exc).__name__, 'visual')
            return False
        finally:
            if wait:
                metrics['visual_wait_seconds'] += time.perf_counter() - before
        metrics['visual_append_seconds'] += elapsed
        if ctx.visual_invalidated:
            return False
        ctx.processed += count
        # stats takes the feature cache lock. Read it only after completion,
        # never from a health publication while another append owns that lock.
        ctx.feature_stats = dict(ctx.features.stats)
        return True

    reason = 'session_limit'
    try:
        while time.monotonic() - started < duration:
            clock = control()
            now = time.time()
            if not clock or not clock.get('active'):
                reason = 'control_stopped'
                break
            validate_control(session_id, clock)
            if clock['expires_at'] < now:
                reason = 'lease_expired'
                break
            if epoch != clock.get('clock_epoch', 0):
                initialize(clock)
                snapshot('starting')
            permit = float(clock['position'])
            if previous_permit is not None and permit < previous_permit - .001:
                raise ValueError('Backward replay clock requires a new clock_epoch')
            if previous_permit is None or permit > previous_permit + 1e-8:
                last_progress_at = time.monotonic()
                has_progress = True
            previous_permit = permit
            barrier_was_waiting = startup_waiting
            startup_waiting = clock.get('startup_waiting', False)
            if startup_waiting or barrier_was_waiting:
                # Local readiness can precede the slowest shard by >30s. The
                # viewer must remain paused until the backend's whole-group
                # barrier opens; keep that bounded wait out of the pause timer.
                last_progress_at = time.monotonic()
            elif (startup_complete or has_progress) and time.monotonic() - last_progress_at > 30:
                reason = 'paused'
                break
            # Timestamp the first permission report for each completed endpoint.
            for ctx in contexts.values():
                if permit + 1e-8 >= ctx.next_end / 15:
                    availability.setdefault(ctx.next_end, now)

            updates = {m['match_id']: m for m in clock.get('sources', [])}
            for key, ctx in contexts.items():
                harvest_visual(ctx)
                if key in updates and isinstance(updates[key].get('source_url'), str):
                    # Refreshed presigned URLs are used on recovery; a healthy
                    # decoder stays open rather than restarting on each refresh.
                    ctx.source['source_url'] = updates[key]['source_url']
                if ctx.future is not None and ctx.future.done():
                    future, ctx.future = ctx.future, None
                    try:
                        result = future.result()
                        if ctx.opening:
                            ctx.cursor, ctx.opening = result, False
                            ctx.status = 'healthy'
                        else:
                            if len(ctx.samples) + len(result) > 32:
                                raise RuntimeError('Source queue exceeds bounded capacity')
                            ctx.samples.extend(result)
                            ctx.last_decode_at = time.monotonic()
                            metrics['max_queue_samples'] = max(metrics['max_queue_samples'], len(ctx.samples))
                    except EOFError:
                        ctx.status, ctx.opening = 'eof', False
                    except Exception as exc:  # noqa: BLE001 -- one failed source must not stop other matches.
                        fail_source(ctx, getattr(exc, 'media_error_type', type(exc).__name__),
                                    getattr(exc, 'media_error_phase', 'reader'))
                if (ctx.status == 'error' and ctx.failures <= 2 and ctx.visual_future is None
                        and time.monotonic() >= ctx.retry_at):
                    retire(ctx)
                    ctx.cursor, ctx.future, ctx.last_read_permit = None, None, None
                    ctx.samples.clear()
                    base = math.ceil(permit / 2 - 1e-8) * 30
                    ctx.features.reset_epoch(f'{epoch}:{ctx.failures}')
                    ctx.processed, ctx.scheduled, ctx.next_end = base, base, base + 75
                    ctx.visual_invalidated = False
                    ctx.feature_stats = dict(ctx.features.stats)
                    ctx.opening, ctx.status = True, 'starting'
                    scores.pop(key, None)
                    ctx.future = readers.submit(open_cursor, ctx.source['source_url'],
                        float(ctx.source.get('offset_seconds', 0)), base, permit)
                if (ctx.cursor is not None and ctx.future is None and ctx.status not in ('eof', 'error')
                        and len(ctx.samples) <= 16
                        and (ctx.last_read_permit != permit
                             or (ctx.cursor.next_index + .5) / 15 <= permit + 1e-8)):
                    # Audio must advance to an exact endpoint even when the
                    # next video interval centre is beyond the permission clock.
                    ctx.last_read_permit = permit
                    ctx.future = readers.submit(pull, ctx, permit)

            ready_for_viewer = startup_ready()
            if ready_for_viewer and not startup_complete:
                startup_complete = True
                metrics['startup_ready_seconds'] = max(0.0, time.monotonic() - initialization_started)
                metrics['startup_ready_epoch'] = epoch
                last_progress_at = time.monotonic()
            if ((not startup_complete or startup_waiting or barrier_was_waiting)
                    and time.monotonic() - initialization_started >= min(120, duration)):
                return snapshot('error', 'startup_timeout')
            if ready_for_viewer and not scores and time.monotonic() - last_publish >= .5:
                snapshot('ready')
            available = []
            # Round robin across logical matches; do not append beyond a pending
            # completed window or evict its first rows before assembling it.
            progress = True
            while len(available) < 16 and progress:
                progress = False
                for ctx in contexts.values():
                    selected = sum(item[0] is ctx for item in available)
                    if (ctx.samples and ctx.status != 'error'
                            and ctx.scheduled + selected < ctx.next_end):
                        available.append((ctx, ctx.samples.popleft()))
                        progress = True
                        if len(available) == 16:
                            break
            force_endpoint = any(permit >= ctx.next_end / 15 and ctx.scheduled +
                                 sum(item[0] is ctx for item in available) >= ctx.next_end
                                 for ctx in contexts.values())
            if available:
                first_pending = first_pending or time.monotonic()
            if available and (len(available) == 16 or force_endpoint or time.monotonic() - first_pending >= 1):
                groups = defaultdict(list)
                for ctx, sample in available:
                    groups[sample.rgb.shape].append((ctx, sample))
                appended = defaultdict(list)
                for group in groups.values():
                    before = time.perf_counter()
                    detections = detector.detect([sample.rgb for _, sample in group])
                    metrics['detector_seconds'] += time.perf_counter() - before
                    metrics['detector_frames'] += len(group)
                    metrics['detector_batches'] += 1
                    for (ctx, sample), raw in zip(group, detections, strict=True):
                        appended[ctx.source['match_id']].append((ctx, sample, raw))
                for rows in appended.values():
                    ctx = rows[0][0]
                    # GPU has just processed the next batch while the previous
                    # CPU measurements were running. Preserve per-match order
                    # before submitting its next job; never enqueue two jobs.
                    if not harvest_visual(ctx, wait=True):
                        continue
                    ctx.visual_count = len(rows)
                    ctx.scheduled += len(rows)
                    ctx.visual_future = visual.submit(append_visual, ctx, rows)
                    metrics['visual_jobs'] += 1
                    metrics['max_visual_pending'] = max(metrics['max_visual_pending'],
                        sum(c.visual_future is not None for c in contexts.values()))
                first_pending = None
            else:
                # Nothing was analyzed; put selected rows back in source order.
                for ctx, sample in reversed(available):
                    ctx.samples.appendleft(sample)

            for ctx in contexts.values():
                harvest_visual(ctx)
            ready = [ctx for ctx in contexts.values() if ctx.visual_future is None
                     and not ctx.visual_invalidated and ctx.processed == ctx.next_end
                     and permit + 1e-8 >= ctx.next_end / 15 and ctx.cursor is not None]
            tensors, completed, assembly_inputs = {}, [], []
            for ctx in ready:
                start_window, end_window = (ctx.next_end - 75) / 15, ctx.next_end / 15
                try:
                    audio = ctx.cursor.audio_window(start_window, end_window)
                except Exception as exc:  # noqa: BLE001 -- never fabricate a window for unavailable audio.
                    # Native audio may still be advancing in its reader job.
                    # Missing coverage never becomes fabricated silence/scores.
                    if time.time() - availability.get(ctx.next_end, time.time()) > 3:
                        fail_source(ctx, type(exc).__name__, 'audio_window')
                    continue
                assembly_inputs.append((ctx, ctx.next_end, audio))
            if assembly_inputs:
                # All image measurements for these contexts have completed.
                # Submit the cohort before waiting so independent window-local
                # tracking/team/interpolation can use the bounded CPU pool.
                # Main processing resumes only after this cohort is harvested;
                # no context can append beyond its unassembled endpoint.
                before = time.perf_counter()
                assembly_jobs = [(ctx, visual.submit(assemble_visual, ctx, end_index, audio))
                                 for ctx, end_index, audio in assembly_inputs]
                metrics['max_assembly_pending'] = max(metrics['max_assembly_pending'], len(assembly_jobs))
                for ctx, future in assembly_jobs:
                    try:
                        features, elapsed = future.result()
                    except Exception as exc:  # noqa: BLE001 -- a failed window cannot fabricate a score.
                        metrics['window_assembly_failures'] += 1
                        fail_source(ctx, type(exc).__name__, 'assembly')
                        continue
                    metrics['window_assembly_task_seconds'] += elapsed
                    ctx.feature_stats = dict(ctx.features.stats)
                    tensors[ctx.source['match_id']] = features
                    completed.append(ctx)
                metrics['window_assembly_seconds'] += time.perf_counter() - before
            if tensors:
                before = time.perf_counter()
                if audio_contract == 'sampleclock-v3':
                    # Predictor timestamps describe the original source, while
                    # published observation clocks remain replay-relative.
                    endpoints = {ctx.source['match_id']: float(ctx.source.get('offset_seconds', 0))
                                 + ctx.next_end / 15 for ctx in completed}
                    values = scorer.predict_scores(tensors, endpoint_seconds=endpoints, half=1)
                else:
                    values = scorer.predict_scores(tensors)
                classifier_seconds = time.perf_counter() - before
                metrics['classifier_seconds'] += classifier_seconds
                produced = time.time()
                for ctx in completed:
                    key, end = ctx.source['match_id'], ctx.next_end / 15
                    ctx.sequence += 1
                    latency = produced - availability.get(ctx.next_end, produced)
                    metrics['deadline_misses'] += int(latency >= 3)
                    metrics['windows'] += 1
                    scores[key] = {'session_id': session_id, 'match_id': key,
                        'clock_epoch': epoch, 'sequence': ctx.sequence,
                        'model_version': spec['model_version'], 'score_kind': score_kind,
                        'score': float(values[key]), 'status': 'healthy',
                        'observation_start': end - 5, 'observation_end': end,
                        'source_timestamp': float(ctx.source.get('offset_seconds', 0)) + end,
                        'observation_available_at': availability.get(ctx.next_end, produced),
                        'produced_at': produced, 'latency_seconds': latency,
                        'timings': {**ctx.features.last_result.timings, 'classifier': classifier_seconds}}
                    ctx.next_end += 30
                snapshot('running')
            elif time.monotonic() - last_publish >= 1:
                snapshot('running' if scores else 'ready' if startup_ready() else 'starting')
            if contexts and all((ctx.status == 'eof' or (ctx.status == 'error' and ctx.failures > 2))
                                and not ctx.samples and ctx.visual_future is None for ctx in contexts.values()):
                reason = 'all_sources_ended'
                break
            time.sleep(.01)
        if reason == 'session_limit' and (not startup_complete or startup_waiting):
            return snapshot('error', 'startup_timeout')
        return snapshot('stopped', reason)
    except Exception as exc:  # noqa: BLE001 -- sanitize arbitrary detector/decoder exceptions.
        snapshot('error', type(exc).__name__)
        raise RuntimeError(f'Replay session failed ({type(exc).__name__})') from None
    finally:
        for ctx in contexts.values():
            retire(ctx)
            discard_visual(ctx)
        readers.shutdown(wait=False, cancel_futures=True)
        visual.shutdown(wait=False, cancel_futures=True)
