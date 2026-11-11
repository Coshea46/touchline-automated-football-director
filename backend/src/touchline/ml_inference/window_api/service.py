"""Synchronous orchestration with explicit per-match failures and bounded fan-out."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import perf_counter

from touchline.ml_inference.models.classifiers import MockInterestScorer, validate_scores
from touchline.ml_inference.window_api.modal_client import ModalFeatureExtractor, ModalInterestScorer
from touchline.ml_inference.window_api.scoring import choose_match
from touchline.ml_inference.features.validation import validate_features, validate_inputs, validate_video_inputs

logger = logging.getLogger(__name__)


class InferenceService:
    """Dependency-injectable implementation; the public function only returns an ID.

    Invalid inputs/configuration/classifiers raise. Failed individual extractions
    are omitted with warnings; if none succeed, retain current_match_id explicitly.
    Injected extractors must be thread-safe and enforce their own I/O timeouts.
    """

    def __init__(self, config, *, extractor=None, scorer=None):
        self.config, self.extractor = config, extractor
        if config.mode == "real":
            if isinstance(scorer, MockInterestScorer):
                raise ValueError("Mock scorer cannot be used in real mode")
            self.scorer = scorer if scorer is not None else ModalInterestScorer(config)
        else:
            self.scorer = scorer if scorer is not None else MockInterestScorer()
            if config.mode == "mock" and not isinstance(self.scorer, MockInterestScorer):
                raise ValueError("Fast mock mode requires an explicit MockInterestScorer")
            logger.warning("EXPLICIT %s MODE: scores are synthetic, with no predictive meaning", config.mode)

    def select(self, current_match_id, match_frames, match_audio):
        validate_inputs(current_match_id, match_frames, match_audio, max_bytes=self.config.max_window_bytes)
        return self._select(current_match_id, match_frames, match_audio)

    def select_video_only(self, current_match_id, match_frames):
        if self.config.mode == "real":
            raise ValueError("Real classifier inference requires synchronized audio")
        validate_video_inputs(current_match_id, match_frames, max_bytes=self.config.max_window_bytes)
        return self._select(current_match_id, match_frames, None)

    def _select(self, current_match_id, match_frames, match_audio):
        if not match_frames:
            return current_match_id
        started = perf_counter()
        if self.config.mode == "mock":
            scores = self.scorer.score_ids(sorted(match_frames))
        else:
            # Configuration/checkpoint failures never become a synthetic prediction or fallback.
            prepare = getattr(self.scorer, "prepare", None)
            if prepare is not None:
                prepare()
            extractor = self.extractor if self.extractor is not None else ModalFeatureExtractor(self.config)
            prepare_extractor = getattr(extractor, "prepare", None)
            if prepare_extractor is not None:
                prepare_extractor()
            features = {}
            pool = ThreadPoolExecutor(max_workers=self.config.max_inflight,
                                      thread_name_prefix="matchpulse-inference")
            futures = {pool.submit(extractor.extract, key, match_frames[key], match_audio[key])
                       if match_audio is not None else pool.submit(extractor.extract, key, match_frames[key]): key
                       for key in sorted(match_frames)}
            timed_out = False
            try:
                remaining = max(0, self.config.request_timeout_seconds - (perf_counter() - started))
                for future in as_completed(futures, timeout=remaining):
                    key = futures[future]
                    try:
                        value = future.result()
                        validate_features(value, missing_audio=match_audio is None)
                        features[key] = value
                    except Exception as exc:  # noqa: BLE001 - isolate independent worker failures
                        # Do not log array contents, signed URLs, or provider exception text.
                        logger.warning("Extraction omitted match %r (%s)", key, type(exc).__name__)
            except TimeoutError:
                timed_out = True
                logger.warning("Extraction deadline reached; unfinished matches are omitted")
                cancel = getattr(extractor, "cancel_pending", None)
                if cancel is not None:
                    cancel()
            finally:
                for future in futures:
                    future.cancel()
                pool.shutdown(wait=not timed_out, cancel_futures=True)
            if not features:
                logger.warning("All extractions failed; retaining current_match_id without a prediction")
                return current_match_id
            scores = validate_scores(self.scorer.predict_scores(features), features)
        selected = choose_match(current_match_id, scores, self.config.staying_bonus)
        logger.info("Selection completed: mode=%s matches=%d seconds=%.6f",
                    self.config.mode, len(scores), perf_counter() - started)
        return selected
