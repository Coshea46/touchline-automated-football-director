"""Historical complete-window deployment/real-footage validation controller.

Only matchpulse-inference can be addressed. Never imports research orchestration.
Private local artifacts and the isolated client environment are ignored by Git.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from touchline.ml_inference.config import APP_NAME
from touchline.paths import INFERENCE_ARTIFACTS_DIR, PROJECT_ROOT
from touchline.config.cloud import window_environment as environment
from touchline.tools.support.window_budget import BudgetedMethod, WindowValidationBudget
from touchline.tools.support.storage import atomic_json

ARTIFACTS = INFERENCE_ARTIFACTS_DIR / "t4-av-validation"


class TaskBudget(WindowValidationBudget):
    def __init__(self):
        super().__init__(ARTIFACTS)


def save(name, value):
    atomic_json(ARTIFACTS / name, value, unique=False, trailing_newline=False)


def deploy():
    if APP_NAME != "matchpulse-inference":
        raise RuntimeError("Unexpected deployment target")
    budget = TaskBudget()
    if sum(row["name"] == "image-build-and-deployment" for row in budget.data["entries"]) >= 2:
        raise RuntimeError("Two bounded T4 deployment attempts already used; inspect failures")
    budget.reserve("image-build-and-deployment", 0.60)
    attempt = sum(row["name"] == "image-build-and-deployment" for row in budget.data["entries"])
    started = time.perf_counter()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    # No footage is uploaded by deploy. Image inputs exclude all local artifacts/envs.
    log_name = f"deployment-{attempt:03d}.log"
    with (ARTIFACTS / log_name).open("w", encoding="utf-8") as log:
        result = subprocess.run(
            [sys.executable, "-B", "-m", "modal", "deploy", "-m", "touchline.ml_inference.deployment.modal_app"],
            cwd=PROJECT_ROOT, env=environment(), stdout=log, stderr=subprocess.STDOUT,
            timeout=1200, check=False,
        )
    save("deployment.json", {"app": APP_NAME, "exit_code": result.returncode,
                             "wall_seconds": time.perf_counter() - started,
                             "gpu": "T4", "max_containers": 1, "min_containers": 0,
                             "scaledown_seconds": 5, "log": log_name})
    if result.returncode:
        raise RuntimeError("Inference deployment failed; inspect the owned deployment log")
    print("Deployed matchpulse-inference; no footage processing started", flush=True)


def load_football_window(path, start):
    """PTS interval-centre sampling, with original pixels and no audio transfer."""
    import av
    import numpy as np

    targets = start + (np.arange(75, dtype=np.float64) + 0.5) / 15
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        origin = float((stream.start_time or 0) * stream.time_base)
        duration = (float(stream.duration * stream.time_base) if stream.duration is not None
                    else container.duration / av.time_base)
        if duration < start + 5 - 1e-6:
            raise ValueError("Source does not cover five complete seconds")
        if start:
            container.seek(int((origin + max(0, start - 0.25)) / stream.time_base),
                           stream=stream, backward=True)
        output = np.empty((75, stream.height, stream.width, 3), dtype=np.uint8)
        previous, source_pts, index = None, [], 0
        for frame in container.decode(stream):
            if frame.pts is None or frame.is_corrupt:
                raise ValueError("Invalid source video observation")
            pts = float(frame.pts * stream.time_base) - origin
            while index < 75 and targets[index] < pts - 1e-8:
                if previous is None or targets[index] - previous[0] > 0.21:
                    raise ValueError("Source coverage gap at a target timestamp")
                output[index] = previous[1].to_ndarray(format="rgb24")
                source_pts.append(previous[0])
                index += 1
            if index == 75:
                break
            previous = (pts, frame)
        if index < 75 and previous is not None:
            coverage = previous[0] + float(previous[1].duration * stream.time_base)
            while index < 75 and targets[index] < coverage + 1e-8:
                output[index] = previous[1].to_ndarray(format="rgb24")
                source_pts.append(previous[0])
                index += 1
        if index != 75:
            raise ValueError("Source ends before the final interval centre")
    return output, {
        "source_basename": path.name, "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "start_seconds": start, "shape": list(output.shape), "dtype": str(output.dtype),
        "sampling": "latest source PTS <= start+(i+0.5)/15; original RGB pixels",
        "target_timestamps": targets.tolist(), "source_timestamps": source_pts,
        "input_sha256": hashlib.sha256(memoryview(output).cast("B")).hexdigest(),
        "audio": "not supplied; research missing-audio representation",
    }


def validate(research_root):
    from touchline.tools.legacy.t4_audio_video import validate as run

    run(research_root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["deploy", "validate"])
    parser.add_argument("--allow-cloud", action="store_true")
    parser.add_argument("--research-root", type=Path,
                        help="Explicit research checkout containing authorized validation recordings")
    args = parser.parse_args()
    if not args.allow_cloud:
        parser.error("Cloud execution must be explicitly authorized")
    if args.action == "validate" and args.research_root is None:
        parser.error("Validation requires an explicit --research-root")
    if args.action == "deploy":
        deploy()
    else:
        try:
            validate(args.research_root)
        except Exception as exc:
            # Keep actionable metadata without copying exception text, SDK blob
            # URLs, credentials or footage into durable artifacts.
            save("failure.json", {"passed": False, "error_type": type(exc).__name__,
                                  "time": time.time(),
                                  "note": "Inspect private app logs and budget call IDs; do not retry blindly"})
            raise


if __name__ == "__main__":
    main()
