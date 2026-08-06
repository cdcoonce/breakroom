from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_pretty_json(path: Path, obj: Any) -> None:
    """Write `obj` as indented, sorted-key JSON with a trailing newline, UTF-8 encoded.

    Shared by the four snapshot-style writers (worldstate, init, tick, secrets) so the
    serialization format can't drift between them.
    """
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
