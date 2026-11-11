"""Atomic JSON receipts shared by operational commands and ledger profiles."""

import json
from pathlib import Path
from uuid import uuid4


def atomic_json(path, data, *, unique=True, trailing_newline=True):
    """Preserve each ledger's existing temporary-file and serialization format."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = (path.with_name(path.name + "." + uuid4().hex + ".tmp") if unique else
                 path.with_suffix(path.suffix + ".tmp"))
    text = json.dumps(data, indent=2, allow_nan=False)
    temporary.write_text(text + ("\n" if trailing_newline else ""), encoding="utf-8")
    temporary.replace(path)
