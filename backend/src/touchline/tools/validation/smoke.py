"""Explicit paid-cloud smoke check; never collected by the local test suite."""

import argparse
from time import perf_counter

import numpy as np

from touchline.ml_inference.config import InferenceConfig
from touchline.ml_inference.window_api.modal_client import ModalFeatureExtractor
from touchline.ml_inference.features.validation import validate_features


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-cloud", action="store_true",
                        help="Run only after explicit approval for this GPU operation")
    args = parser.parse_args()
    if not args.allow_cloud:
        parser.error("Explicit approval is required before --allow-cloud")
    config = InferenceConfig.from_env()
    if config.mode != "features_mock":
        parser.error("Use MATCHPULSE_ML_MODE=features_mock for this extraction-only check")
    client = ModalFeatureExtractor(config)
    frames = np.zeros((75, 90, 160, 3), dtype=np.uint8)
    started = perf_counter()
    try:
        features = client.extract("synthetic-smoke", frames, np.zeros(80000, np.float32))
        validate_features(features, missing_audio=False)
        print({"shape": features.shape, "dtype": str(features.dtype),
               "seconds": perf_counter() - started,
               "note": "Synthetic extraction smoke check, not anticipation accuracy"})
    finally:
        client.cancel_pending()


if __name__ == "__main__":
    main()
