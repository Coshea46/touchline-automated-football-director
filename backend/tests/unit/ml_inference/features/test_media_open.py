"""Independent decoder opens overlap without losing ownership or diagnostics."""

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from touchline.ml_inference.features.media import MediaCursor
from tests.support.ml_inference.media import AV, PRIVATE_SOURCE, cursor


def test_video_and_audio_open_overlap_with_thread_local_log_capture():
    class ParallelAV(AV):
        def __init__(self):
            super().__init__()
            self.barrier = threading.Barrier(2)
            self.threads = set()
            self.capture = threading.local()
            owner = self
            class Capture:
                def __init__(self, *, local):
                    assert local is True
                def __enter__(self):
                    owner.capture.active = True
                def __exit__(self, *args):
                    owner.capture.active = False
            self.logging = SimpleNamespace(Capture=Capture)

        def open(self, source, **kwargs):
            assert self.capture.active
            self.threads.add(threading.get_ident())
            self.barrier.wait(timeout=1)  # Serial opens cannot satisfy this barrier.
            return super().open(source, **kwargs)

    worker, av = cursor(ParallelAV())
    try:
        assert len(av.threads) == 2 and len(av.containers) == 2
        assert worker._video_container is not worker._audio_container
        assert not any(thread.name.startswith('replay-av-open') for thread in threading.enumerate())
        worker.prime(0)
        stats = worker.stats
        keys = ('video_open_seconds', 'audio_open_seconds', 'parallel_open_seconds',
                'video_seek_seconds', 'audio_seek_seconds', 'prime_seconds', 'cursor_init_seconds')
        assert all(isinstance(stats[key], float) and np.isfinite(stats[key]) and stats[key] >= 0 for key in keys)
        assert stats['cursor_init_seconds'] >= stats['parallel_open_seconds']
        assert stats['prime_seconds'] > 0 and 'secret-test-token' not in str(stats)
        assert worker.next_index == 0 and [frame.index for frame in av.video if frame.converted] == [0]
    finally:
        worker.close()


@pytest.mark.parametrize('failed_kind', ['video', 'audio'])
def test_failed_open_joins_and_closes_late_successful_sibling(monkeypatch, failed_kind):
    av = AV()
    failed, sibling_finished = threading.Event(), threading.Event()
    original = MediaCursor._av_call

    def controlled(self, function, *args, _phase='avio', **kwargs):
        if _phase == failed_kind + '_open':
            failed.set()
            raise OSError(PRIVATE_SOURCE)
        if _phase in ('video_open', 'audio_open'):
            assert failed.wait(1)
            time.sleep(.02)  # The successful handle arrives after its sibling failed.
            result = original(self, function, *args, _phase=_phase, **kwargs)
            sibling_finished.set()
            return result
        return original(self, function, *args, _phase=_phase, **kwargs)

    monkeypatch.setattr(MediaCursor, '_av_call', controlled)
    with pytest.raises(RuntimeError, match='Media open failed') as failure:
        cursor(av)
    assert sibling_finished.is_set() and len(av.containers) == 1 and av.containers[0].closed
    assert failure.value.media_error_type == 'OSError'
    assert failure.value.media_error_phase == failed_kind + '_open'
    assert PRIVATE_SOURCE not in str(failure.value) and 'secret-test-token' not in str(failure.value)
    assert not any(thread.name.startswith('replay-av-open') for thread in threading.enumerate())


def test_two_failed_opens_preserve_video_error_precedence_despite_reverse_completion(monkeypatch):
    audio_failed = threading.Event()
    original = MediaCursor._av_call
    def controlled(self, function, *args, _phase='avio', **kwargs):
        if _phase == 'audio_open':
            audio_failed.set()
            raise OSError(PRIVATE_SOURCE)
        if _phase == 'video_open':
            assert audio_failed.wait(1)
            raise ValueError(PRIVATE_SOURCE)
        return original(self, function, *args, _phase=_phase, **kwargs)
    monkeypatch.setattr(MediaCursor, '_av_call', controlled)
    with pytest.raises(RuntimeError, match='Media open failed') as failure:
        cursor()
    assert failure.value.media_error_type == 'ValueError'
    assert failure.value.media_error_phase == 'video_open'
    assert 'secret-test-token' not in str(failure.value)
    assert not any(thread.name.startswith('replay-av-open') for thread in threading.enumerate())
