from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TAIL_CHUNK_SIZE = 4096


def _next_sequence(events_path: Path) -> int:
    if not events_path.exists():
        return 1
    file_size = events_path.stat().st_size
    if file_size == 0:
        return 1
    chunk_size = _TAIL_CHUNK_SIZE
    with events_path.open("rb") as handle:
        while True:
            read_size = min(chunk_size, file_size)
            handle.seek(file_size - read_size)
            chunk = handle.read(read_size)
            lines = chunk.splitlines()
            if read_size == file_size or len(lines) > 1:
                break
            chunk_size *= 2
    for line in reversed(lines):
        if line.strip():
            last_event = json.loads(line)
            return int(last_event["sequence"]) + 1
    return 1


def append_event(world: Path, event: dict[str, Any]) -> dict[str, Any]:
    events_path = world / "events.jsonl"
    sequence = _next_sequence(events_path)
    event = {**event, "sequence": sequence}
    with events_path.open("a", encoding="utf-8") as events:
        events.write(json.dumps(event, sort_keys=True) + "\n")
    return event
