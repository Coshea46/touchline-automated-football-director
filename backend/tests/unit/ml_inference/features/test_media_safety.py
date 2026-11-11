"""Offline tests for unit ml_inference features test_media_safety."""


from types import SimpleNamespace
import pytest
from touchline.ml_inference.features.media import MediaCursor, MediaTimelineError
from tests.support.ml_inference.media import AV, cursor, PRIVATE_SOURCE


def test_invalid_video_timestamps_and_gaps_fail_explicitly():
    av = AV()
    av.video[5].pts = av.video[4].pts
    worker, _ = cursor(av)
    with pytest.raises(MediaTimelineError, match='strictly increasing'):
        worker.pull(1)


def test_decoded_resolution_growth_is_rejected_before_rgb_conversion():
    av = AV()
    av.video[2].width, av.video[2].height = 3840, 2160
    worker, _ = cursor(av)
    with pytest.raises(MediaTimelineError, match='changes dimensions'):
        worker.pull(.2)
    assert av.video[2].converted == 0
    assert all(frame.converted == 0 for frame in av.video[2:])


def test_lookahead_metadata_can_be_read_but_future_pixels_are_not_converted():
    worker, av = cursor()
    worker.pull(0.1)
    assert av.video[3].converted == 0  # .12s lookahead for .10s target.
    assert max(f.index for f in av.video if f.converted) <= 2


def test_low_level_errors_hide_private_source_and_close_opened_containers():
    class BrokenAV(AV):
        def open(self, source, **kwargs):
            if self.containers:
                raise OSError(PRIVATE_SOURCE)
            return super().open(source, **kwargs)

    av = BrokenAV()
    with pytest.raises(RuntimeError, match='Media open failed') as raised:
        cursor(av)
    assert 'secret' not in str(raised.value) and PRIVATE_SOURCE not in str(raised.value)
    assert av.containers[0].closed


def test_ffmpeg_logging_is_captured_on_open_seek_and_lazy_decode_reads():
    class CapturedAV(AV):
        def __init__(self):
            super().__init__()
            owner = self
            self.capture_depth, self.captures = 0, 0

            class Capture:
                def __init__(self, *, local):
                    assert local is True

                def __enter__(self):
                    owner.capture_depth += 1
                    owner.captures += 1

                def __exit__(self, *args):
                    owner.capture_depth -= 1

            self.logging = SimpleNamespace(Capture=Capture)

        def open(self, source, **kwargs):
            assert self.capture_depth > 0
            container = super().open(source, **kwargs)
            original_seek = container.seek

            def seek(*args, **kwargs):
                assert self.capture_depth > 0
                return original_seek(*args, **kwargs)

            def decode(stream):
                assert self.capture_depth > 0

                def frames():
                    for frame in container.frames:
                        assert self.capture_depth > 0
                        yield frame

                return frames()

            container.seek, container.decode = seek, decode
            return container

    worker, av = cursor(CapturedAV())
    worker.pull(0.2)
    assert av.capture_depth == 0 and av.captures >= 10
    worker.close()


@pytest.mark.parametrize('source', ['file:///secret', 'ftp://host/media', 'https://user:pass@host/video', ''])
def test_sources_are_http_only_without_explicit_local_fixture_opt_in(source):
    with pytest.raises(ValueError):
        MediaCursor(source, av_module=AV())
