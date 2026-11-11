"""Validate the fixed window contract without guessing colour order or timing."""

import numpy as np


def validate_match_id(value, name="match_id"):
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must be nonempty")


def validate_window(frames, *, max_bytes=None):
    if not isinstance(frames, np.ndarray):
        raise TypeError("Each video window must be a NumPy array")
    if frames.ndim != 4 or frames.shape[0] != 75 or frames.shape[-1] != 3:
        raise ValueError("Expected exactly (75,H,W,3) RGB frames at 15 FPS for five seconds")
    if min(frames.shape[1:3]) < 16:
        raise ValueError("Spatial dimensions must each be >=16 pixels, as required by the extractor")
    if frames.dtype != np.uint8:
        raise TypeError("Video frames must have dtype uint8")
    if max_bytes is not None and frames.nbytes > max_bytes:
        raise ValueError("Video window exceeds configured max_window_bytes")


def validate_audio(audio):
    if not isinstance(audio, np.ndarray):
        raise TypeError("Each audio window must be a NumPy array")
    if audio.dtype != np.float32:
        raise TypeError("Audio must be float32 normalized PCM at 16000 Hz")
    if audio.shape not in ((80000,), (80000, 2)):
        raise ValueError("Audio must be exactly five seconds at 16000 Hz: (80000,) or (80000,2)")
    if not np.isfinite(audio).all():
        raise ValueError("Audio must contain only finite samples")
    if np.any((audio < -1) | (audio > 1)):
        raise ValueError("Audio amplitude must be within [-1,1]")


def validate_video_inputs(current_match_id, match_frames, *, max_bytes=None):
    validate_match_id(current_match_id, "current_match_id")
    if not isinstance(match_frames, dict):
        raise TypeError("match_frames must be a dictionary")
    for match_id, frames in match_frames.items():
        validate_match_id(match_id)
        validate_window(frames, max_bytes=max_bytes)


def validate_inputs(current_match_id, match_frames, match_audio, *, max_bytes=None):
    validate_video_inputs(current_match_id, match_frames, max_bytes=max_bytes)
    if not isinstance(match_audio, dict):
        raise TypeError("match_audio must be a dictionary")
    if set(match_frames) != set(match_audio):
        raise ValueError("Video and audio must contain exactly the same match IDs")
    for key, audio in match_audio.items():
        validate_match_id(key)
        validate_audio(audio)


def validate_features(features, *, missing_audio=None):
    if not isinstance(features, np.ndarray):
        raise TypeError("Extracted features must be a NumPy array")
    if features.shape != (75, 215) or features.dtype != np.float32:
        raise ValueError("Expected raw (75,215) float32 features")
    if not np.isfinite(features).all():
        raise ValueError("Extracted features must be finite")
    if missing_audio and np.any(features[:, 210:215] != 0):
        raise ValueError("Frame-only input requires the extractor's five zero missing-audio channels")
    if not np.isin(features[:, 214], [0, 1]).all():
        raise ValueError("Audio availability must be binary")
    if missing_audio is False and not np.all(features[:, 214] == 1):
        raise ValueError("Full-audio inference requires audio_available=1 in every interval")
