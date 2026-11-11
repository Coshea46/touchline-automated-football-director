"""Load ignored local backend configuration without logging or evaluating values."""

import os
import re
from pathlib import Path

from touchline.paths import PROJECT_ROOT

PROVIDER_KEYS = {"ELEVENLABS_API_KEY", "FOOTBALL_DATA_API_KEY", "GUARDIAN_API_KEY"}
CONFIGURATION_KEYS = PROVIDER_KEYS | {"S3_BUCKET_NAME"}


def load_local_environment(path=None, env=None):
    path = Path(path) if path is not None else PROJECT_ROOT / ".env"
    env = os.environ if env is None else env
    if not path.is_file():
        return 0
    if path.stat().st_size > 65536:
        raise ValueError("Local configuration exceeds the size limit")
    loaded = 0
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError(f"Invalid local configuration at line {line_number}")
        if name not in CONFIGURATION_KEYS and not name.startswith(("MATCHPULSE_", "AWS_", "MODAL_")):
            continue
        value = value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise ValueError(f"Unclosed local configuration value at line {line_number}")
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        if name not in env:
            env[name] = value
            loaded += 1
    return loaded
