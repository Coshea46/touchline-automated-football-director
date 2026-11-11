"""An opt-in CPU-only Modal check of the private bucket; no deployment required."""

import json
from pathlib import Path

import modal

from touchline.config.video_sources import VIDEO_BUCKET, VIDEO_REGION, VIDEO_READER_ROLE

app = modal.App("touchline-storage-check")
package = Path(__file__).resolve().parents[2]
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("boto3==1.28.62", "numpy>=1.24,<3", "websockets==11.0.3")
    .env({"PYTHONPATH": "/opt/touchline", "AWS_DEFAULT_REGION": VIDEO_REGION,
          "AWS_ROLE_ARN": VIDEO_READER_ROLE, "S3_BUCKET_NAME": VIDEO_BUCKET,
          "MATCHPULSE_SOURCE_ACCESS": "presigned", "MATCHPULSE_CAPTIONS_ENABLED": "false",
          "MATCHPULSE_MONITOR": "false", "MATCHPULSE_PROJECT_ROOT": "/tmp/touchline-storage-check"})
    .add_local_dir(str(package), "/opt/touchline/touchline",
                   ignore=["**/__pycache__/**", "**/*.pyc"])
)


@app.function(image=image, cpu=0.25, memory=256, timeout=90,
              min_containers=0, max_containers=1)
def inspect_private_sources():
    from touchline.tools.storage.verify_sources import verify_sources

    return verify_sources()


@app.local_entrypoint()
def main(allow_read: bool = False):
    if not allow_read:
        raise ValueError("Pass --allow-read to run the bounded CPU-only S3 check")
    report = inspect_private_sources.remote()
    print(json.dumps(report, indent=2, allow_nan=False))
    if not report["passed"]:
        raise RuntimeError("Private source verification failed; see safe error codes above")
