"""Verify a built backend wheel and caption IPC startup without provider access.

Run with the backend development interpreter (including pip), after building:
python scripts/verify_backend_package.py backend/dist/touchline_backend-0.1.0-py3-none-any.whl
"""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from zipfile import ZipFile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--caption-python", type=Path,
                        help="Also check an installed isolated caption interpreter without audio")
    args = parser.parse_args()
    wheel_path = args.wheel.resolve(strict=True)
    with ZipFile(wheel_path) as wheel:
        names = set(wheel.namelist())
        required = {
            "touchline/data/matches.json",
            "touchline/captions/live/worker.py",
            "touchline/captions/live/engine.py",
            "touchline/captions/identity.py",
            "touchline/api/http.py",
            "touchline/api/routes.py",
            "touchline/api/websocket.py",
            "touchline/replay/captions.py",
            "touchline/replay/monitoring.py",
            "touchline/config/video_sources.py",
            "touchline/storage/s3.py",
            "touchline/tools/storage/verify_sources.py",
            "touchline/tools/storage/modal_probe.py",
            "touchline/ml_inference/deployment/requirements-worker.txt",
            "touchline/ml_inference/runtime/audio_sampleclock_v3.py",
            "touchline/ml_inference/runtime/matchpulse-0.1.0+autonomous1-py3-none-any.whl",
            "touchline/ml_inference/runtime/selected_release/smoke-raw.npy",
        }
        if required - names:
            raise RuntimeError(f"Wheel is missing runtime assets: {sorted(required - names)}")
        retired = {
            "touchline/captions/live_process.py",
            "touchline/football_platform/server.py",
            "touchline/websocket_manager.py",
            "touchline/ml_inference/modal_app.py",
            "touchline/ml_inference/replay_runner.py",
        }
        if retired & names:
            raise RuntimeError("Wheel contains stale files from an earlier build; clean backend/build first")
        if any(name.startswith("tests/") or "/tests/" in name or "/.env" in name for name in names):
            raise RuntimeError("Wheel includes tests or local configuration")

    with tempfile.TemporaryDirectory(prefix="touchline-wheel-") as temporary:
        scratch = Path(temporary)
        installed = scratch / "installed"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("MATCHPULSE_", "REACT_APP_", "AWS_", "MODAL_"))
               and key not in {"ELEVENLABS_API_KEY", "FOOTBALL_DATA_API_KEY", "GUARDIAN_API_KEY", "PYTHONPATH"}}
        env.update(MATCHPULSE_PROJECT_ROOT=str(scratch), PIP_DISABLE_PIP_VERSION_CHECK="1")
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
                        "--quiet", "--target", str(installed), str(wheel_path)],
                       check=True, cwd=scratch, env=env, timeout=90)
        # An isolated process must import the installed wheel, even when the
        # caller's interpreter also has an editable checkout installed.
        check = """
import importlib.metadata, json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import touchline
assert pathlib.Path(touchline.__file__).is_relative_to(pathlib.Path(sys.argv[1]))
from touchline.match_metadata import METADATA_PATH, metadata_for_source
catalog = json.loads(METADATA_PATH.read_text(encoding='utf-8'))
assert len(catalog['matches']) == 7
for item in catalog['matches']:
    assert metadata_for_source(item['match_id'], item['source_key'])
from touchline.replay.sources import SourceRegistry
from touchline.config.video_sources import VIDEO_BUCKET, VIDEO_REGION
registry = SourceRegistry.from_env({})
assert registry.bucket == VIDEO_BUCKET
assert registry.region == VIDEO_REGION
assert registry.access == 'presigned'
from touchline.storage import S3Access
from touchline.tools.storage.verify_sources import verify_sources
assert callable(verify_sources)
from touchline.ml_inference.models.release import verify_release_support
from touchline.ml_inference.features.audio import verify_reference_decoder
verify_release_support()
verify_reference_decoder()
distribution = importlib.metadata.distribution('touchline-backend')
for entry in distribution.entry_points:
    if entry.group == 'console_scripts':
        assert callable(entry.load())
import runpy
runpy.run_module('touchline.captions.live.worker', run_name='__main__')
"""
        subprocess.run([sys.executable, "-I", "-B", "-c", check, str(installed)],
                       input="", text=True, check=True, cwd=scratch, env=env, timeout=30)
        if args.caption_python:
            caption_python = args.caption_python.resolve(strict=True)
            caption_check = """
import importlib.metadata, runpy
assert importlib.metadata.version('websockets') == '13.1'
import elevenlabs
runpy.run_module('touchline.captions.live.worker', run_name='__main__')
"""
            subprocess.run([str(caption_python), "-I", "-B", "-c", caption_check],
                           input="", text=True, check=True, cwd=scratch, env=env, timeout=30)
            print("Isolated caption runtime: ElevenLabs SDK, websockets 13.1 and EOF startup passed.")
    print("Backend wheel: assets, private-source defaults, entry points, match catalog and caption IPC startup passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
