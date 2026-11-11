"""Native decoder replacement preserves the submitted source-time boundary."""

from fractions import Fraction

import numpy as np
import pytest

from touchline.captions.live.audio import ProgressiveAudioReader

av = pytest.importorskip("av")


@pytest.mark.parametrize("rate", [44100, 48000])
def test_native_reader_reopens_at_fractional_last_submitted_pcm_end(tmp_path, rate):
    path = tmp_path / "synthetic.mkv"
    with av.open(str(path), "w") as output:
        video = output.add_stream("mpeg4", rate=25)
        video.width = video.height = 16
        video.pix_fmt = "yuv420p"
        audio = output.add_stream("pcm_s16le", rate=rate)
        audio.layout = "mono"
        count = rate // 25
        for index in range(30):
            frame = av.VideoFrame.from_ndarray(np.zeros((16, 16, 3), np.uint8), format="rgb24")
            frame.pts, frame.time_base = index, Fraction(1, 25)
            for packet in video.encode(frame):
                output.mux(packet)
            time = (np.arange(count) + index * count) / rate
            wave = np.rint(12000 * np.sin(2 * np.pi * 431 * time)).astype(np.int16)
            frame = av.AudioFrame.from_ndarray(wave[None], format="s16", layout="mono")
            frame.pts, frame.time_base, frame.sample_rate = index * count, Fraction(1, rate), rate
            for packet in audio.encode(frame):
                output.mux(packet)
        for packet in video.encode(None):
            output.mux(packet)
        for packet in audio.encode(None):
            output.mux(packet)

    first = ProgressiveAudioReader(str(path), .123457)
    try:
        submitted = [first.pull(permit) for permit in (.23, .31, .42)]
        end = next(chunk.end_seconds for chunk in reversed(submitted) if chunk is not None)
    finally:
        first.close()
    renewed = ProgressiveAudioReader(str(path), end)
    try:
        chunk = renewed.pull(.65)
        assert chunk is not None
        assert abs(chunk.start_seconds - end) <= 1.01 / 16000
        assert chunk.start_seconds >= end - 1e-7
        assert chunk.end_seconds <= .65 + 1e-7
        assert len(chunk.pcm) > 0
    finally:
        renewed.close()
