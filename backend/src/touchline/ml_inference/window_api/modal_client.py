"""Lazy Modal adapters. No SDK import, authentication or RPC during package import."""

import importlib
import logging
from threading import Event, Lock
from time import perf_counter

from touchline.ml_inference.models.classifiers import validate_scores
from touchline.ml_inference.config import APP_NAME, SCHEMA_VERSION
from touchline.ml_inference.window_api.transport import encode_request, encode_window, payload_bytes
from touchline.ml_inference.features.validation import validate_features

logger = logging.getLogger(__name__)


def _sdk():
    try:
        return importlib.import_module("modal")
    except ImportError as exc:
        raise RuntimeError("Install the isolated ML package's [modal] extra to use extraction") from exc


def _cancel(handle):
    try:
        # Cancel only this input, never an app or other container's work.
        handle.cancel(terminate_containers=False)
    except Exception as exc:  # noqa: BLE001 - cancellation must not mask the original failure
        logger.warning("Could not cancel inference input (%s)", type(exc).__name__)


def _get_result(handle, sdk, timeout):
    try:
        return handle.get(timeout=timeout)
    except Exception as exc:
        timeout_types = [TimeoutError]
        for name in ("TimeoutError", "FunctionTimeoutError"):
            kind = getattr(sdk.exception, name, None)
            if isinstance(kind, type):
                timeout_types.append(kind)
        if isinstance(exc, tuple(timeout_types)):
            _cancel(handle)
            raise TimeoutError("Modal inference input exceeded its timeout") from exc
        raise


class ModalFeatureExtractor:
    """Request-scoped dispatch/cancellation; deployed containers retain warm models."""

    def __init__(self, config, *, sdk=None):
        self.config, self.sdk = config, sdk
        self._worker = None
        self._lock = Lock()
        self._cancelled = Event()
        self._active = {}
        self.last_metrics = {}

    def prepare(self):
        with self._lock:
            self._get_worker()
        return self

    def _get_worker(self):
        if self._worker is None:
            self.sdk = self.sdk or _sdk()
            cls = self.sdk.Cls.from_name(APP_NAME, "FeatureWorker").with_options(
                gpu=self.config.gpu, max_containers=self.config.worker_concurrency,
                timeout=self.config.worker_timeout_seconds,
                scaledown_window=self.config.scaledown_seconds,
            )
            self._worker = cls(fp16=self.config.fp16, max_window_bytes=self.config.max_window_bytes)
        return self._worker

    def extract(self, match_id, frames, audio=None):
        if self._cancelled.is_set():
            raise TimeoutError("Inference request was cancelled")
        started = perf_counter()
        payload = (encode_request(match_id, frames, audio, max_bytes=self.config.max_window_bytes)
                   if audio is not None else encode_window(frames, max_bytes=self.config.max_window_bytes))
        encode_seconds = perf_counter() - started
        logger.info("Lossless transport: raw=%d compressed=%d encode_seconds=%.6f",
                    frames.nbytes, payload_bytes(payload), perf_counter() - started)
        with self._lock:
            if self._cancelled.is_set():
                raise TimeoutError("Inference request was cancelled before dispatch")
            handle = self._get_worker().extract.spawn(payload)
            self._active[id(handle)] = handle
        submit_seconds = perf_counter() - started - encode_seconds
        try:
            waiting = perf_counter()
            result = _get_result(handle, self.sdk, self.config.call_timeout_seconds)
            remote_metrics = {}
            if audio is not None:
                if not isinstance(result, dict) or result.get("match_id") != match_id:
                    raise ValueError("Returned feature match ID does not match the request")
                remote_metrics = result.get("metrics", {})
                result = result["features"]
            validate_features(result, missing_audio=audio is None)
            self.last_metrics[match_id] = {
                "raw_bytes": frames.nbytes, "compressed_bytes": payload_bytes(payload),
                "encode_seconds": encode_seconds, "submit_seconds": submit_seconds,
                "result_wait_seconds": perf_counter() - waiting,
                "total_seconds": perf_counter() - started,
                "audio_raw_bytes": audio.nbytes if audio is not None else 0,
                "audio_compressed_bytes": len(payload["audio"]["block"]) if audio is not None else 0,
                "encoding_seconds": payload.get("encoding_seconds"), "remote": remote_metrics,
            }
            return result
        finally:
            with self._lock:
                self._active.pop(id(handle), None)

    def cancel_pending(self):
        with self._lock:
            self._cancelled.set()
            handles = list(self._active.values())
        for handle in handles:
            _cancel(handle)


class ModalInterestScorer:
    """CPU worker owns the real bundle, saved preprocessing and batch prediction."""

    def __init__(self, config, *, sdk=None):
        self.config, self.sdk = config, sdk
        self._worker = None
        self._lock = Lock()
        self._ready = False

    def prepare(self):
        with self._lock:
            if not self._ready:
                self.sdk = self.sdk or _sdk()
                self._worker = self.sdk.Cls.from_name(APP_NAME, "ClassifierWorker").with_options(
                    timeout=self.config.classifier_timeout_seconds, max_containers=1,
                    scaledown_window=self.config.scaledown_seconds,
                )(checkpoint_name=self.config.checkpoint)
                # Detect a missing/invalid real bundle before paying for feature extraction.
                metadata = _get_result(self._worker.ready.spawn(), self.sdk, self.config.call_timeout_seconds)
                required = {"schema": SCHEMA_VERSION, "input_shape": [75, 215],
                            "audio_required": True, "model_input_size": 215}
                if not isinstance(metadata, dict) or any(metadata.get(key) != value for key, value in required.items()):
                    raise ValueError("Deployed classifier worker has an incompatible schema")
                self._ready = True
        return self

    def predict_scores(self, features):
        for value in features.values():
            validate_features(value, missing_audio=False)
        self.prepare()
        handle = self._worker.predict.spawn(features)
        return validate_scores(
            _get_result(handle, self.sdk, self.config.call_timeout_seconds), features
        )
