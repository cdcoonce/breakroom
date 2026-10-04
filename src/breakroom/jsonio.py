from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_pretty_json(path: Path, obj: Any) -> None:
    """Write `obj` as indented, sorted-key JSON with a trailing newline, UTF-8 encoded.

    Writes to a temporary file in the same directory then atomically replaces the
    target, so a failure during write never leaves the destination partially written.
    """
    dirname = path.parent
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".tmp", dir=dirname, encoding="utf-8", delete=False
    ) as tmp:
        tmp.write(json.dumps(obj, indent=2, sort_keys=True) + "\n")
        tmp_path = tmp.name
    os.replace(tmp_path, path)
