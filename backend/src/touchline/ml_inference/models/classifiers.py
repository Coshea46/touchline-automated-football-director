"""Interchangeable mock and research portable-bundle adapters."""

import hashlib
import importlib
import re
from pathlib import Path
from threading import Lock
from typing import Protocol

import numpy as np

from touchline.ml_inference.config import SCHEMA_VERSION, finite_number
from touchline.ml_inference.models.release import (
    CHECKPOINT_SHA256,
    LEGACY_CHECKPOINT_SHA256,
    LEGACY_MODEL_VERSION,
    NEW_MODEL_VERSION,
    PARAMETER_COUNT,
    RELEASE_ROOT,
    SELECTED_HEAD,
    SMOKE_ATOL,
    SOURCE_SHA256,
    verify_release_support,
)
from touchline.ml_inference.features.validation import validate_features, validate_match_id

MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024


def checkpoint_sha256(path):
    path = Path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_CHECKPOINT_BYTES:
        raise ValueError("Portable checkpoint must be a nonempty file no larger than 64 MiB")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        total = 0
        while block := stream.read(1024 * 1024):
            total += len(block)
            if total > MAX_CHECKPOINT_BYTES:
                raise ValueError("Portable checkpoint exceeds 64 MiB")
            digest.update(block)
    return digest.hexdigest()


def validate_replay_classifier(scorer, *, model_version=None):
    """Audit a trusted portable full-audio GRU without predicting or normalizing.

    The research loader still owns model construction, strict state loading and
    normalization semantics. This stricter replay gate leaves the legacy generic
    adapter's supported model architectures unchanged.
    """
    if isinstance(scorer, SelectedTemporalScorer):
        scorer.prepare()
        if model_version is not None and model_version != scorer.model_version:
            raise ValueError("Selected replay model version mismatch")
        return dict(scorer.readiness)
    bundle_sha256 = checkpoint_sha256(scorer.path)
    scorer.prepare()
    classifier = scorer._classifier
    info = classifier.metadata
    if not isinstance(info, dict):
        raise TypeError("Portable classifier metadata must be a mapping")
    architecture = info.get("architecture", {})
    if (info.get("checkpoint_version") != "matchpulse-interest-v1"
            or info.get("feature_schema_version") != SCHEMA_VERSION
            or info.get("input_shape") != [75, 215] or info.get("sampling_fps") != 15
            or info.get("clip_duration_seconds") != 5 or info.get("model_input_size") != 215
            or info.get("feature_indices") != list(range(215))):
        raise ValueError("Replay requires a portable full-audio matchpulse-215-v1 checkpoint")
    if (not isinstance(architecture, dict) or architecture.get("kind") != "gru"
            or architecture.get("input_size") != 215 or architecture.get("hidden_size") != 64):
        raise ValueError("Replay requires the full-audio hidden-size-64 GRU")
    model = getattr(classifier, "model", None)
    gru = getattr(model, "gru", None)
    if (getattr(gru, "input_size", None) != 215 or getattr(gru, "hidden_size", None) != 64
            or type(getattr(gru, "num_layers", None)) is not int or gru.num_layers != 1
            or getattr(gru, "bidirectional", None) is not False
            or getattr(gru, "batch_first", None) is not True
            or getattr(model, "training", None) is not False):
        raise ValueError("Loaded replay model must be an eval-mode one-layer unidirectional GRU")
    normalization = info.get("normalization")
    if (not isinstance(normalization, dict) or normalization.get("version") != "masked-channel-zscore-v1"
            or normalization.get("schema") != SCHEMA_VERSION or normalization.get("fitted_split") != "train"):
        raise ValueError("Replay requires saved training-only normalization provenance")
    try:
        mean = np.asarray(normalization.get("mean"), dtype=np.float64)
        scale = np.asarray(normalization.get("scale"), dtype=np.float64)
        count = np.asarray(normalization.get("count"), dtype=np.float64)
        if (mean.shape != (215,) or scale.shape != (215,) or count.shape != (215,)
                or not np.isfinite(mean).all() or not np.isfinite(scale).all()
                or not np.isfinite(count).all() or (scale <= 0).any() or (count < 0).any()
                or (count != np.floor(count)).any()):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise ValueError("Replay normalization must describe all 215 finite channels") from exc
    audit_sha = info.get("dataset_audit_sha256")
    if not isinstance(audit_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", audit_sha):
        raise ValueError("Replay requires dataset audit SHA256 provenance")
    threshold = info.get("decision_threshold")
    if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not np.isfinite(threshold) or not 0 <= threshold <= 1
            or info.get("threshold_selection_split") != "valid"):
        raise ValueError("Replay requires a validation-selected operating threshold")
    version = model_version or scorer.path.parent.name
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", version):
        raise ValueError("Invalid replay model version")
    return {"schema": SCHEMA_VERSION, "input_shape": [75, 215], "audio_required": True,
            "model_input_size": 215, "model_version": version,
            "bundle_sha256": bundle_sha256,
            "architecture": {"kind": "gru", "input_size": 215, "hidden_size": 64,
                             "num_layers": 1, "bidirectional": False},
            "normalization": {"version": normalization["version"], "fitted_split": "train", "channels": 215},
            "dataset_audit_sha256": audit_sha.lower(), "decision_threshold": float(threshold),
            "threshold_selection_split": "valid"}


class InterestScorer(Protocol):
    def predict_scores(self, features: dict[str, np.ndarray]) -> dict[str, float]: ...


def validate_scores(scores, expected_ids):
    if not isinstance(scores, dict) or set(scores) != set(expected_ids):
        raise ValueError("Classifier must return exactly one score per supplied match ID")
    result = {}
    for match_id, score in scores.items():
        value = finite_number(score, "interest score")
        if value > 1:
            raise ValueError("Interest score must lie in [0,1]")
        result[match_id] = value
    return result


class MockInterestScorer:
    """EXPLICIT MOCK: synthetic constant/configured scores, no predictive meaning."""

    def __init__(self, scores=None, *, default_score=0.5):
        self.scores = dict(scores or {})
        self.default_score = default_score
        validate_scores({"default": default_score}, ["default"])
        for match_id in self.scores:
            validate_match_id(match_id)
        validate_scores(self.scores, self.scores)

    def score_ids(self, match_ids):
        return validate_scores(
            {key: self.scores.get(key, self.default_score) for key in match_ids}, match_ids
        )

    def predict_scores(self, features):
        for value in features.values():
            validate_features(value)
        return self.score_ids(list(features))


class ResearchInterestScorer:
    """Load the real research export; delegate normalization/selection/batching unchanged.

    This adapter runs inside the compatible worker environment (Python >=3.11).
    loader injection supports local tests without Torch or a trained checkpoint.
    """

    def __init__(self, checkpoint, *, device="cpu", loader=None):
        self.path = Path(checkpoint)
        self.device, self.loader = device, loader
        self._classifier = None
        self._lock = Lock()
        self.audio_contract = "legacy-gru"

    def prepare(self):
        with self._lock:
            if self._classifier is None:
                if not self.path.is_file():
                    raise FileNotFoundError(f"Portable classifier bundle does not exist: {self.path}")
                loader = self.loader
                if loader is None:
                    from matchpulse import load_interest_classifier

                    loader = load_interest_classifier
                classifier = loader(self.path, device=self.device)
                metadata = classifier.metadata
                if (metadata.get("feature_schema_version") != SCHEMA_VERSION
                        or metadata.get("input_shape") != [75, 215]
                        or metadata.get("sampling_fps") != 15
                        or metadata.get("clip_duration_seconds") != 5):
                    raise ValueError("Classifier does not implement the five-second MatchPulse schema")
                if (metadata.get("model_input_size") != 215
                        or metadata.get("feature_indices") != list(range(215))):
                    raise ValueError("Production requires a full-audio 215-channel classifier bundle")
                self._classifier = classifier
        return self

    def predict_scores(self, features, *, endpoint_seconds=None, half=1):
        for match_id, value in features.items():
            validate_match_id(match_id)
            validate_features(value, missing_audio=False)
        self.prepare()
        ids = sorted(features)
        if not ids:
            return {}
        # Raw tensors go straight to the research API. It owns saved normalization,
        # 215/210 channel selection, model.eval(), inference_mode() and sigmoid.
        scores = np.asarray(self._classifier.predict_batch(np.stack([features[key] for key in ids])))
        if scores.shape != (len(ids),):
            raise ValueError("Research classifier returned an invalid batch dimension")
        return validate_scores(dict(zip(ids, scores)), ids)


class SelectedTemporalScorer:
    """Exact released predictor, with immutable identity and a real readiness smoke.

    Extraction repairs the v3 audio channels once. This adapter forwards the raw
    observation unchanged; the release owns representation, normalization and GRU.
    Temporal prediction has no history buffer or cross-match commit side effects.
    """

    audio_contract = "sampleclock-v3"

    def __init__(self, checkpoint, *, model_version=NEW_MODEL_VERSION, device="cpu"):
        if model_version != NEW_MODEL_VERSION:
            raise ValueError("Unknown selected replay model version")
        # The published SelectedPredictor constructs its temporal classifier on CPU.
        # Keep that API unchanged; YOLO remains on the worker's GPU independently.
        if device != "cpu":
            raise ValueError("Released selected predictor requires CPU inference")
        self.path = Path(checkpoint)
        self.model_version = model_version
        self._predictor = None
        self._observation = None
        self._lock = Lock()
        self.readiness = None

    def prepare(self):
        with self._lock:
            if self._predictor is not None:
                return self
            digest = checkpoint_sha256(self.path)
            if digest != CHECKPOINT_SHA256:
                raise ValueError("Selected checkpoint SHA256 mismatch")
            observation, verification = verify_release_support()
            from matchpulse.research.predictor import (
                Observation,
                load_selected_predictor,
            )

            for module_name, expected in SOURCE_SHA256.items():
                module = importlib.import_module(module_name)
                if checkpoint_sha256(module.__file__) != expected:
                    raise ValueError("Selected runtime source SHA256 mismatch")
            predictor = load_selected_predictor(
                self.path.parent, expected_manifest_sha256=CHECKPOINT_SHA256,
                head=SELECTED_HEAD, release_root=self.path.parent,
            )
            classifier = predictor.model
            model = classifier.model
            metadata = classifier.metadata
            gru = getattr(model, "gru", None)
            count = sum(parameter.numel() for parameter in model.parameters())
            if (predictor.kind != "raw215" or classifier.selected_head != SELECTED_HEAD
                    or count != PARAMETER_COUNT or model.training is not False
                    or getattr(gru, "input_size", None) != 68
                    or getattr(gru, "hidden_size", None) != 16
                    or getattr(gru, "num_layers", None) != 1
                    or getattr(gru, "batch_first", None) is not True
                    or getattr(gru, "bidirectional", None) is not False):
                raise ValueError("Selected temporal model architecture mismatch")
            raw = np.load(RELEASE_ROOT / "smoke-raw.npy", allow_pickle=False)
            validate_features(raw)
            smoke_observation = Observation(
                observation["match_id"], observation["half"],
                observation["observation_end_seconds"], raw215=raw,
            )
            score = validate_scores({"smoke": predictor.predict(smoke_observation)}, ["smoke"])["smoke"]
            expected = verification["expected_three_scores"][verification["head_order"].index(SELECTED_HEAD)]
            difference = abs(score - expected)
            if difference > SMOKE_ATOL:
                raise ValueError("Selected release fixture score parity failed")
            threshold = finite_number(classifier.threshold, "selected threshold")
            if threshold > 1:
                raise ValueError("Selected threshold must lie in [0,1]")
            self.readiness = {
                "schema": SCHEMA_VERSION, "input_shape": [75, 215], "audio_required": True,
                "audio_contract": self.audio_contract, "model_input_size": 215,
                "model_version": self.model_version, "bundle_sha256": digest,
                "architecture": {"kind": "temporal_gru", "input_size": 68, "hidden_size": 16,
                                 "num_layers": 1, "bidirectional": False, "heads": 3,
                                 "parameter_count": count},
                "selected_head": SELECTED_HEAD, "score_semantics": metadata["score_semantics"],
                "source_sha256": dict(SOURCE_SHA256),
                "normalization": {"version": "observed-frame-values-validity-bits",
                                  "fitted_split": "train", "channels": 34},
                "decision_threshold": threshold, "threshold_selection_split": "valid",
                "smoke": {"passed": True, "expected_score": expected, "actual_score": score,
                          "absolute_difference": difference, "atol": SMOKE_ATOL, "rtol": 0},
            }
            self._observation = Observation
            self._predictor = predictor
        return self

    def predict_scores(self, features, *, endpoint_seconds=None, half=1):
        if not isinstance(features, dict):
            raise TypeError("Features must be a mapping of match IDs to observations")
        for match_id, raw in features.items():
            validate_match_id(match_id)
            validate_features(raw)  # Explicit availability masks may contain legitimate gaps.
        if not features:
            return {}
        endpoints = endpoint_seconds if isinstance(endpoint_seconds, dict) else {
            match_id: endpoint_seconds for match_id in features
        }
        if set(endpoints) != set(features):
            raise ValueError("Exact source endpoint per supplied observation required")
        for endpoint in endpoints.values():
            if finite_number(endpoint, "observation endpoint") < 5:
                raise ValueError("Completed five-second observation endpoint required")
        self.prepare()
        with self._lock:
            scores = {
                match_id: self._predictor.predict(self._observation(
                    match_id, half, endpoints[match_id], raw215=features[match_id],
                )) for match_id in sorted(features)
            }
        return validate_scores(scores, features)


def load_replay_scorer(checkpoint_path, *, model_version=NEW_MODEL_VERSION, device="cpu"):
    """Choose one explicit audited version; never fall back after a load failure."""
    if model_version == NEW_MODEL_VERSION:
        return SelectedTemporalScorer(checkpoint_path, model_version=model_version, device=device).prepare()
    if model_version == LEGACY_MODEL_VERSION:
        if checkpoint_sha256(checkpoint_path) != LEGACY_CHECKPOINT_SHA256:
            raise ValueError("Legacy rollback checkpoint SHA256 mismatch")
        scorer = ResearchInterestScorer(checkpoint_path, device=device)
        scorer.readiness = validate_replay_classifier(scorer, model_version=model_version)
        return scorer
    raise ValueError("Unsupported replay model version; select the release or explicit legacy rollback")
