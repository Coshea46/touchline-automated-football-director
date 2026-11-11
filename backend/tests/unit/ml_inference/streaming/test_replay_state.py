"""Offline tests for unit ml_inference streaming test_replay_state."""


import contextvars
import time
from threading import Event
import numpy as np
import pytest
from touchline.ml_inference.streaming.state import LatestStateChannel


def control():
    return {'active': True, 'playing': True, 'position': 4.75,
            'expires_at': 1234.5, 'sources': [{'match_id': 'a'}]}


def test_stalled_publication_coalesces_to_one_latest_item_without_blocking_processing():
    entered, release = Event(), Event()
    published = []

    def publisher(snapshot):
        published.append(snapshot)
        if snapshot['sequence'] == 0:
            entered.set()
            release.wait(2)

    channel = LatestStateChannel(control, publisher, poll_seconds=.005)
    try:
        channel.start()
        channel.wait_initial(1)
        channel.publish({'sequence': 0})
        assert entered.wait(1)
        started = time.monotonic()
        for sequence in range(1, 101):
            channel.publish({'sequence': sequence})
            assert channel.control()['position'] == 4.75
        assert time.monotonic() - started < .5
        assert channel.stats['pending'] == channel.stats['max_pending'] == 1
        assert channel.stats['inflight'] == channel.stats['max_inflight'] == 1
        assert channel.stats['coalesced_snapshots'] == 99
        release.set()
        channel.close(timeout=1, flushlatest=True)
        assert [item['sequence'] for item in published] == [0, 100]
        assert channel.stats['publications'] == 2
        assert not channel.stats['publication_thread_alive']
    finally:
        release.set()
        channel.close(timeout=1)


def test_control_and_publication_stalls_share_one_bounded_shutdown_deadline():
    read_entered, publication_entered, release = Event(), Event(), Event()

    def reader():
        read_entered.set()
        release.wait(2)
        return control()

    def publisher(snapshot):
        publication_entered.set()
        release.wait(2)

    channel = LatestStateChannel(reader, publisher)
    try:
        channel.start()
        channel.publish({'state': 'stopped'})
        assert read_entered.wait(1) and publication_entered.wait(1)
        started = time.monotonic()
        assert channel.control() is None
        channel.close(timeout=.02)
        assert time.monotonic() - started < .2
        assert channel.stats['control_thread_alive']
        assert channel.stats['publication_thread_alive']
        assert channel._control_thread.daemon and channel._publication_thread.daemon
    finally:
        release.set()
        channel.close(timeout=1)


def test_original_viewer_clock_and_expiry_survive_read_failures_without_extrapolation():
    failed = Event()
    reads = 0

    def reader():
        nonlocal reads
        reads += 1
        if reads == 1:
            return control()
        failed.set()
        raise OSError('https://private.invalid/video?secret=not-for-logs')

    with LatestStateChannel(reader, lambda value: None, poll_seconds=.005) as channel:
        assert channel.wait_initial(1) == control()
        assert failed.wait(1)
        for _ in range(20):
            assert channel.control() == control()
        assert channel.control()['expires_at'] == 1234.5
        assert channel.stats['last_control_error'] == 'OSError'
        assert channel.stats['control_errors'] > 0
        assert 'not-for-logs' not in str(channel.stats)


def test_separate_copied_contexts_inherit_modal_input_context_without_sharing_mutations():
    input_context = contextvars.ContextVar('test_modal_input_context', default='missing')
    token = input_context.set('active-modal-input')
    reader_values, publisher_values = [], []
    published = Event()

    def reader():
        reader_values.append(input_context.get())
        input_context.set('reader-private-value')
        return control()

    def publisher(snapshot):
        publisher_values.append(input_context.get())
        input_context.set('publisher-private-value')
        published.set()

    try:
        with LatestStateChannel(reader, publisher, poll_seconds=.005) as channel:
            channel.wait_initial(1)
            channel.publish({'state': 'running'})
            assert published.wait(1)
            assert reader_values[0] == publisher_values[0] == 'active-modal-input'
            assert input_context.get() == 'active-modal-input'
    finally:
        input_context.reset(token)


def test_snapshots_and_cached_control_are_copied_at_the_producer_boundary():
    entered, release = Event(), Event()
    received = []

    def publisher(snapshot):
        entered.set()
        release.wait(2)
        received.append(snapshot)

    channel = LatestStateChannel(control, publisher)
    try:
        channel.start()
        cached = channel.wait_initial(1)
        cached['sources'][0]['match_id'] = 'mutated'
        assert channel.control()['sources'][0]['match_id'] == 'a'
        snapshot = {'scores': [{'score': .5}]}
        channel.publish(snapshot)
        assert entered.wait(1)
        snapshot['scores'][0]['score'] = 999
        release.set()
        channel.close(timeout=1)
        assert received == [{'scores': [{'score': .5}]}]
    finally:
        release.set()
        channel.close(timeout=1)


@pytest.mark.parametrize('bad', [np.zeros((75, 215), np.float32), object(), (1, 2),
                               float('nan'), float('inf'), {1: 'bad-key'}])
def test_publications_reject_arrays_tensors_and_nonprimitive_values_before_enqueue(bad):
    with LatestStateChannel(control, lambda value: None) as channel:
        with pytest.raises((TypeError, ValueError)):
            channel.publish({'payload': bad})
        assert channel.stats['pending'] == channel.stats['max_pending'] == 0


def test_reader_errors_and_invalid_payloads_have_bounded_sanitized_diagnostics():
    with LatestStateChannel(lambda: {'frames': np.zeros(10)}, lambda value: None,
                            poll_seconds=.005) as channel:
        with pytest.raises(TimeoutError, match='Initial replay control'):
            channel.wait_initial(.02)
        assert channel.control() is None
        assert channel.stats['last_control_error'] == 'TypeError'
        assert channel.stats['control_errors'] >= 1


def test_publication_failure_records_only_class_and_can_publish_newer_state():
    failed, succeeded = Event(), Event()

    def publisher(snapshot):
        if snapshot['sequence'] == 1:
            failed.set()
            raise OSError('https://private.invalid/video?secret=hidden')
        succeeded.set()

    with LatestStateChannel(control, publisher) as channel:
        channel.publish({'sequence': 1})
        assert failed.wait(1)
        channel.publish({'sequence': 2})
        assert succeeded.wait(1)
    assert channel.stats['publication_errors'] == 1
    assert channel.stats['last_publication_error'] == 'OSError'
    assert 'secret' not in str(channel.stats)


def test_close_can_drop_pending_state_and_never_wait_for_a_stalled_publication():
    entered, release = Event(), Event()
    published = []

    def publisher(snapshot):
        published.append(snapshot['sequence'])
        entered.set()
        release.wait(2)

    channel = LatestStateChannel(control, publisher)
    try:
        channel.start()
        channel.publish({'sequence': 1})
        assert entered.wait(1)
        channel.publish({'sequence': 2})
        channel.close(timeout=.01, flushlatest=False)
        assert channel.stats['pending'] == 0
        release.set()
        channel.close(timeout=1)
        assert published == [1]
    finally:
        release.set()
        channel.close(timeout=1)


def test_successful_missing_control_is_returned_without_a_synthetic_clock():
    with LatestStateChannel(lambda: None, lambda value: None) as channel:
        assert channel.wait_initial(1) is None
        assert channel.control() is None


def test_closed_channel_cannot_restart_or_accept_new_state():
    channel = LatestStateChannel(control, lambda value: None)
    channel.close()
    with pytest.raises(RuntimeError, match='closed'):
        channel.start()
    with pytest.raises(RuntimeError, match='closed'):
        channel.publish({'state': 'running'})
