"""Offline tests for unit ml_inference window_api test_transport."""


import copy
import zlib
import numpy as np
import pytest
from touchline.ml_inference.window_api.transport import decode_window, encode_window, payload_bytes


@pytest.mark.parametrize("shape", [(75, 16, 24, 3), (75, 32, 16, 3)])
def test_lossless_roundtrip_including_noncontiguous_rgb(shape):
    frames = np.random.default_rng(42).integers(0, 256, size=shape, dtype=np.uint8)[:, :, ::-1]
    encoded = encode_window(frames)
    restored = decode_window(encoded)
    assert restored.dtype == np.uint8 and restored.flags.c_contiguous
    np.testing.assert_array_equal(restored, frames)
    assert payload_bytes(encoded) == sum(len(b) for b in encoded["blocks"])


@pytest.mark.parametrize("kind", ["version", "shape", "length", "corrupt", "trailing", "bomb", "truncated"])
def test_invalid_or_expanding_payloads(kind):
    encoded = encode_window(np.zeros((75, 16, 16, 3), np.uint8))
    bad = copy.deepcopy(encoded)
    if kind == "version":
        bad["version"] = "unknown"
    elif kind == "shape":
        bad["shape"] = (75, True, 16, 3)
    elif kind == "length":
        bad["blocks"].pop()
    elif kind == "corrupt":
        bad["blocks"][0] = b"not-zlib"
    elif kind == "trailing":
        bad["blocks"][0] += b"extra"
    elif kind == "bomb":
        bad["blocks"][0] = zlib.compress(bytes(100000))
    elif kind == "truncated":
        bad["blocks"][0] = bad["blocks"][0][:-2]
    with pytest.raises(ValueError):
        decode_window(bad)


def test_transport_memory_bounds_both_sides():
    frames = np.zeros((75, 16, 16, 3), np.uint8)
    with pytest.raises(ValueError, match="max_window_bytes"):
        encode_window(frames, max_bytes=1)
    with pytest.raises(ValueError, match="max_window_bytes"):
        decode_window(encode_window(frames), max_bytes=1)
