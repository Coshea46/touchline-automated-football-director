"""Independent inference deployment. Import via Modal CLI only, after approval.

No references to matchpulse-private or its Volumes/leases/controllers are used.
The ordinary public package does not import this module.
"""

import os
from pathlib import Path
from time import perf_counter

import modal

from touchline.ml_inference.config import APP_NAME, MODEL_VOLUME, PACKAGE_ROOT
from touchline.ml_inference.streaming.contract import MAX_GPU_WORKERS

PACKAGE = PACKAGE_ROOT
GPU = os.environ.get("MATCHPULSE_ML_GPU", "T4")
CONTAINERS = int(os.environ.get("MATCHPULSE_ML_WORKER_CONCURRENCY", "1"))
TIMEOUT = int(os.environ.get("MATCHPULSE_ML_WORKER_TIMEOUT_SECONDS", "180"))
SCALEDOWN = int(os.environ.get("MATCHPULSE_ML_SCALEDOWN_SECONDS", "5"))
CLASSIFIER_TIMEOUT = int(os.environ.get("MATCHPULSE_ML_CLASSIFIER_TIMEOUT_SECONDS", "60"))
if min(CONTAINERS, TIMEOUT, SCALEDOWN, CLASSIFIER_TIMEOUT) < 1:
    raise ValueError("Inference deployment resource values must be positive")

app = modal.App(APP_NAME)

if modal.is_local():
    from touchline.ml_inference.deployment.packaging import runtime_artifacts

    wheel, weights = runtime_artifacts(os.environ.get("MATCHPULSE_ML_WEIGHTS"))
    image = (
        modal.Image.debian_slim(python_version="3.13")
        .apt_install("libgl1", "libglib2.0-0")
        .pip_install("torch==2.11.0", "torchvision==0.26.0",
                     index_url="https://download.pytorch.org/whl/cu130")
        .pip_install_from_requirements(str(PACKAGE / "deployment" / "requirements-worker.txt"))
        .add_local_file(str(wheel), "/opt/runtime/" + wheel.name, copy=True)
        .run_commands("python -m pip install --no-deps /opt/runtime/" + wheel.name)
        .add_local_file(str(weights), "/opt/models/yolo26s.pt", copy=True)
        .env({"PYTHONPATH": "/opt/client", "YOLO_CONFIG_DIR": "/tmp/matchpulse-inference-ultralytics",
              "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
              "NUMEXPR_NUM_THREADS": "1"})
        .add_local_file(str(PACKAGE.parent / "__init__.py"), "/opt/client/touchline/__init__.py")
        .add_local_dir(str(PACKAGE), "/opt/client/touchline/ml_inference",
                       ignore=["tests/**", "**/*.whl", "artifacts/**", ".venv*/**",
                               "**/__pycache__/**", "**/*.pyc", ".pytest_cache/**"])
    )
else:
    image = modal.Image.debian_slim(python_version="3.13")

# New inference-only storage, resolved/created by a future authorized deployment.
models = modal.Volume.from_name(MODEL_VOLUME, create_if_missing=True)


def detector_runtime(detector, expected_fp16):
    # Ultralytics setup_model deep-copies the source model. Verify the active
    # prediction backend, whose parameters actually perform inference.
    predictor = detector.model.predictor
    backend = predictor.model
    parameter = next(backend.model.parameters())
    device, dtype = str(parameter.device), str(parameter.dtype)
    if not device.startswith("cuda") or not str(predictor.device).startswith("cuda"):
        raise RuntimeError("Active detector prediction backend did not execute on CUDA")
    if bool(backend.fp16) != expected_fp16 or dtype != ("torch.float16" if expected_fp16 else "torch.float32"):
        raise RuntimeError("Active detector prediction precision differs from configuration")
    return {"detector_device": device, "predictor_device": str(predictor.device),
            "detector_dtype": dtype, "detector_fp16": bool(backend.fp16)}


@app.cls(image=image, gpu=GPU, cpu=(1, 4), memory=(2048, 8192), max_containers=CONTAINERS,
         min_containers=0, buffer_containers=0, scaledown_window=SCALEDOWN,
         timeout=TIMEOUT, startup_timeout=300, retries=0, include_source=False)
class FeatureWorker:
    fp16: bool = modal.parameter(default=True)
    max_window_bytes: int = modal.parameter(default=512 * 1024 * 1024)

    @modal.enter()
    def load(self):
        started = perf_counter()
        import torch

        from touchline.ml_inference.features.extraction import ResearchFeatureExtractor

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA feature extraction is required; no automatic CPU fallback")
        self.extractor = ResearchFeatureExtractor(
            "/opt/models/yolo26s.pt", device="cuda:0", fp16=self.fp16
        ).prepare()
        self.runtime = {
            "gpu": torch.cuda.get_device_name(0), "torch": str(torch.__version__),
            "cuda": torch.version.cuda, "fp16": self.fp16,
            "container_id": os.environ.get("MODAL_TASK_ID"),
        }
        self.initialization_seconds = perf_counter() - started
        self.calls = 0

    @modal.method()
    def extract(self, payload):
        from touchline.ml_inference.window_api.transport import (
            AV_TRANSPORT_VERSION,
            decode_request,
            decode_window,
        )

        started = perf_counter()
        full_audio = isinstance(payload, dict) and payload.get("version") == AV_TRANSPORT_VERSION
        if full_audio:
            match_id, frames, audio = decode_request(payload, max_bytes=self.max_window_bytes)
        else:
            match_id, audio = "window", None  # Explicit legacy test transport only.
            frames = decode_window(payload, max_bytes=self.max_window_bytes)
        decode_seconds = perf_counter() - started
        # No modal.concurrent decorator: each container serializes inputs. The
        # research extractor resets tracking for each complete window.
        self.extractor.clear_cache()
        self.calls += 1
        t = perf_counter()
        features = self.extractor.extract(match_id, frames, audio) if full_audio else self.extractor.extract(match_id, frames)
        if not full_audio:
            return features
        extraction_seconds = perf_counter() - t
        return {"match_id": match_id, "features": features,
                "metrics": {"runtime": {**self.runtime, **detector_runtime(self.extractor._extractor.detector, self.fp16)},
                            "decode_seconds": decode_seconds, "extraction_seconds": extraction_seconds,
                            "initialization_seconds": self.initialization_seconds,
                            "detector_load_seconds": self.extractor._extractor.model_load_seconds,
                            "stage_seconds": self.extractor.last_result.timings,
                            "container_call_number": self.calls,
                            "remote_wall_seconds": perf_counter() - started}}

    @modal.method()
    def diagnose(self, payload, match_id, compare_reference=True):
        return self._diagnose(payload, match_id, compare_reference)

    @modal.method()
    def diagnose_pair(self, payload_a, payload_b):
        """A-B-A on one warm worker; no cross-container state-isolation claim."""
        import numpy as np

        results = []
        for payload, compare in [(payload_a, True), (payload_b, True), (payload_a, False)]:
            result = self._diagnose(payload, payload["match_id"], compare)
            results.append(result)
            if compare and not result["gate"]["passed"]:
                return results
        results[-1]["repeated_A_after_B_equal"] = bool(np.array_equal(results[0]["features"], results[-1]["features"]))
        return results

    def _diagnose(self, payload, match_id, compare_reference=True):
        """Private bounded validation: same adapter plus a fresh research reference."""
        import hashlib
        import importlib.metadata

        import numpy as np
        from matchpulse.features import MatchFeatureExtractor

        from touchline.ml_inference.window_api.transport import decode_request
        from touchline.ml_inference.features.validation import validate_features, validate_match_id

        validate_match_id(match_id)
        started = perf_counter()
        _, frames, audio = decode_request(payload, expected_id=match_id, max_bytes=self.max_window_bytes)
        decode_seconds = perf_counter() - started
        before = hashlib.sha256(memoryview(frames).cast("B")).hexdigest()
        audio_before = hashlib.sha256(memoryview(audio).cast("B")).hexdigest()
        self.extractor.clear_cache()
        self.calls += 1
        t = perf_counter()
        features = self.extractor.extract(match_id, frames, audio)
        extraction_seconds = perf_counter() - t
        result = self.extractor.last_result
        diagnostics = result.diagnostics
        detector_details = detector_runtime(self.extractor._extractor.detector, self.fp16)
        gate = None
        if compare_reference:
            source = self.extractor._extractor
            reference = MatchFeatureExtractor(
                detector=source.detector, detector_config=source.detector_config, config=source.config
            )
            t = perf_counter()
            expected = reference.extract(frames, fps=15, audio=audio, audio_sample_rate=16000)
            validate_features(expected, missing_audio=False)
            reference_seconds = perf_counter() - t
            close = np.isclose(features, expected, atol=2e-5, rtol=1e-5)
            slots_equal = np.array_equal(diagnostics["slot_ids"], reference.last_result.diagnostics["slot_ids"])
            teams_equal = np.array_equal(features[:, :198].reshape(75, 22, 9)[:, :, 8],
                                        expected[:, :198].reshape(75, 22, 9)[:, :, 8])
            gate = {
                "passed": bool(close.all() and slots_equal and teams_equal),
                "atol": 2e-5, "rtol": 1e-5,
                "max_absolute_difference": float(np.max(np.abs(features - expected))),
                "elements_outside_tolerance": int((~close).sum()),
                "slot_ids_equal": bool(slots_equal), "teams_equal": bool(teams_equal),
                "audio_equal": bool(np.array_equal(features[:, 210:], expected[:, 210:])),
                "camera_equal": bool(np.allclose(features[:, 205:210], expected[:, 205:210], atol=2e-5, rtol=1e-5)),
                "reference_shape": list(expected.shape), "reference_dtype": str(expected.dtype),
                "camera_cuts_equal": bool(np.array_equal(features[:, 208], expected[:, 208])),
                "reference_seconds": reference_seconds,
                "reference": "Fresh research MatchFeatureExtractor, identical model/config/GPU/precision",
            }
        after = hashlib.sha256(memoryview(frames).cast("B")).hexdigest()
        return {
            "match_id": match_id, "features": features,
            "runtime": {**self.runtime, **detector_details},
            "initialization_seconds": self.initialization_seconds, "container_call_number": self.calls,
            "decode_seconds": decode_seconds, "extraction_seconds": extraction_seconds,
            "detector_load_seconds": self.extractor._extractor.model_load_seconds,
            "stage_seconds": result.timings, "input_sha256": before,
            "input_unchanged": before == after, "gate": gate,
            "audio_sha256": audio_before,
            "audio_unchanged": audio_before == hashlib.sha256(memoryview(audio).cast("B")).hexdigest(),
            "audio_available_intervals": int(features[:, 214].sum()),
            "audio_feature_nonzero": bool(np.any(features[:, 210:214] != 0)),
            "visible_player_mean": float(features[:, :198].reshape(75, 22, 9)[:, :, 7].sum(axis=1).mean()),
            "ball_visible_samples": int(features[:, 203].sum()),
            "frames_inferred": diagnostics["detector_frames_inferred"],
            "versions": {name: importlib.metadata.version(name)
                         for name in ["numpy", "av", "ultralytics", "opencv-python", "scipy"]},
            "remote_wall_seconds": perf_counter() - started,
        }


@app.cls(image=image, cpu=2, memory=4096, volumes={"/models": models},
         max_containers=1, min_containers=0, buffer_containers=0,
         scaledown_window=SCALEDOWN, timeout=CLASSIFIER_TIMEOUT,
         retries=0, include_source=False)
class ClassifierWorker:
    checkpoint_name: str = modal.parameter(default="classifier.pt")

    @modal.enter()
    def load(self):
        from touchline.ml_inference.models.classifiers import load_replay_scorer

        root = Path("/models").resolve()
        target = (root / self.checkpoint_name).resolve()
        if not target.is_relative_to(root) or target == root:
            raise ValueError("Classifier path must remain inside the inference model Volume")
        models.reload()
        self.scorer = load_replay_scorer(target, model_version=self.checkpoint_name, device="cpu")
        self.readiness = self.scorer.readiness

    @modal.method()
    def ready(self):
        return self.readiness

    @modal.method()
    def predict(self, features, endpoint_seconds=None, half=1):
        if self.scorer.audio_contract == 'sampleclock-v3':
            return self.scorer.predict_scores(features, endpoint_seconds=endpoint_seconds, half=half)
        return self.scorer.predict_scores(features)


@app.cls(image=image, gpu=["T4", "L4"], cpu=(4, 8), memory=(4096, 8192),
         # Ten independent visitors may each own up to four workers. This is
         # pool capacity; each individual run retains its four-worker bound.
         volumes={'/models': models}, max_containers=10 * MAX_GPU_WORKERS, min_containers=0,
         secrets=[modal.Secret.from_name('matchpulse-inference-replay-auth')],
         buffer_containers=0, scaledown_window=5, timeout=4000,
         startup_timeout=300, retries=0, include_source=False)
class ReplayWorker:
    """A single persistent GPU pool, multiplexing independent logical matches."""

    @modal.enter()
    def load(self):
        import cv2
        import numpy as np
        import torch
        from matchpulse.features import DetectorConfig
        from matchpulse.features.detector import YoloDetector

        started = perf_counter()
        if not torch.cuda.is_available():
            raise RuntimeError('Replay monitoring requires CUDA')
        cv2.setNumThreads(1)
        torch.set_num_threads(2)
        self.detector_config = DetectorConfig(weights='/opt/models/yolo26s.pt', device='cuda:0',
            half=True, image_size=640, batch_size=16, confidence=.15, ball_confidence=.08,
            person_class=0, ball_class=32)
        self.detector = YoloDetector(self.detector_config)
        # One startup warmup, before viewers leave the explicit startup buffer.
        # These synthetic images never produce a score or success assertion.
        warmup_start = perf_counter()
        blank = np.zeros((720, 1280, 3), np.uint8)
        self.detector.detect([blank] * 16)
        self.runtime = {'gpu': torch.cuda.get_device_name(0), 'torch': str(torch.__version__),
                        'cuda': torch.version.cuda, 'container_id': os.environ.get('MODAL_TASK_ID'),
                        **detector_runtime(self.detector, True),
                        'warmup_seconds': perf_counter() - warmup_start,
                        'initialization_seconds': perf_counter() - started}
        self.runtime['numerical_threads'] = {key: os.environ.get(key) for key in
            ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS')}
        self.classifiers = {}

    def _scorer(self, spec):
        self._scorer_phase = 'load'
        from touchline.ml_inference.models.classifiers import (
            MockInterestScorer,
            load_replay_scorer,
        )

        if spec['mode'] == 'features_mock':
            return MockInterestScorer(spec.get('mock_scores'))
        self._scorer_phase = 'resolve'
        # Modal's mount can resolve to a different filesystem location. Compare
        # both sides in the same resolved namespace, as ClassifierWorker does.
        root = Path('/models').resolve()
        target = (root / spec['checkpoint']).resolve()
        if not target.is_relative_to(root) or target == root:
            raise ValueError('Checkpoint must remain inside inference-only storage')
        if str(target) not in self.classifiers:
            self._scorer_phase = 'volume_reload'
            models.reload()
            self._scorer_phase = 'load'
            scorer = load_replay_scorer(target, model_version=spec['model_version'], device='cpu')
            self._scorer_phase = 'validate'
            if scorer.readiness.get('model_version') != spec['model_version']:
                raise ValueError('Loaded model version does not match the session')
            if len(self.classifiers) >= 2:
                self.classifiers.pop(next(iter(self.classifiers)))
            self.classifiers[str(target)] = scorer
        scorer = self.classifiers[str(target)]
        self._scorer_phase = 'validate'
        if scorer.readiness.get('model_version') != spec['model_version']:
            raise ValueError('Loaded model version does not match the session')
        return scorer

    @modal.method()
    def run(self, spec):
        from touchline.ml_inference.streaming.contract import REPLAY_DICT, validate_session
        from touchline.ml_inference.streaming.metrics import ResourceSampler
        from touchline.ml_inference.streaming.runner import run_replay_session
        from touchline.ml_inference.streaming.state import LatestStateChannel

        validate_session(spec)
        state = modal.Dict.from_name(REPLAY_DICT, create_if_missing=True)
        session_id = spec['session_id']
        score_key = 'scores:' + session_id + (':' + spec['shard_id'] if 'shard_id' in spec else '')
        try:
            scorer = self._scorer(spec)
        except Exception as exc:  # noqa: BLE001 - sanitize checkpoint errors before compact publication
            import time
            phase = getattr(self, '_scorer_phase', 'load')
            if phase not in ('resolve', 'volume_reload', 'load', 'validate'):
                phase = 'load'
            # Only these application-owned static messages map to public codes.
            # Never forward arbitrary loader/SDK messages containing paths/URIs.
            codes = {
                'Checkpoint must remain inside inference-only storage': 'checkpoint_path_escape',
                'Portable checkpoint must be a nonempty file no larger than 64 MiB': 'checkpoint_file_invalid',
                'Portable checkpoint exceeds 64 MiB': 'checkpoint_file_too_large',
                'Portable classifier metadata must be a mapping': 'checkpoint_metadata_invalid',
                'Replay requires a portable full-audio matchpulse-215-v1 checkpoint': 'checkpoint_schema_mismatch',
                'Replay requires the full-audio hidden-size-64 GRU': 'checkpoint_architecture_mismatch',
                'Loaded replay model must be an eval-mode one-layer unidirectional GRU': 'checkpoint_model_structure',
                'Replay requires saved training-only normalization provenance': 'checkpoint_normalization_provenance',
                'Replay normalization must describe all 215 finite channels': 'checkpoint_normalization_invalid',
                'Replay requires dataset audit SHA256 provenance': 'checkpoint_dataset_provenance',
                'Replay requires a validation-selected operating threshold': 'checkpoint_threshold_invalid',
                'Invalid replay model version': 'checkpoint_version_invalid',
                'Classifier does not implement the five-second MatchPulse schema': 'checkpoint_schema_mismatch',
                'Production requires a full-audio 215-channel classifier bundle': 'checkpoint_audio_schema',
            }
            clock = state.get('control:' + session_id, {})
            result = {'session_id': session_id, 'model_version': spec['model_version'],
                      'clock_epoch': clock.get('clock_epoch', 0), 'worker_sequence': 1,
                      'score_kind': 'model' if spec['mode'] == 'real' else 'mock',
                      'state': 'error', 'reason': 'checkpoint_unavailable',
                      'error_type': type(exc).__name__, 'error_phase': phase,
                      'error_code': codes.get(str(exc), 'checkpoint_' + phase + '_failed'),
                      'produced_at': time.time(), 'scores': [], 'health': {}}
            state.put(score_key, result)
            return result
        with ResourceSampler() as resources, LatestStateChannel(
                lambda: state.get('control:' + session_id, None),
                lambda record: state.put(score_key, record)) as channel:
            channel.wait_initial(timeout=5)
            return run_replay_session(spec, self.detector,
                channel.control, channel.publish, scorer=scorer,
                detector_config=self.detector_config, runtime=self.runtime,
                state_metrics=lambda: {**channel.stats, 'resources': resources.stats})

    @modal.method()
    def validate_sources(self, spec):
        from touchline.ml_inference.streaming.validation import validate_direct_sources

        scorer = self._scorer(spec)
        return validate_direct_sources(spec, self.detector, self.detector_config, self.runtime,
                                       audio_contract=getattr(scorer, 'audio_contract', 'legacy-gru'))

    @modal.asgi_app(requires_proxy_auth=True)
    def web(self):
        from touchline.ml_inference.streaming.asgi import ReplayASGI

        return ReplayASGI(self.detector, self._scorer,
            detector_config=self.detector_config, runtime=self.runtime,
            phase_getter=lambda: getattr(self, '_scorer_phase', 'load'))
