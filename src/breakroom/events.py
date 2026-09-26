from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def append_event(world: Path, event: dict[str, Any]) -> dict[str, Any]:
    events_path = world / "events.jsonl"
    sequence = 1
    if events_path.exists():
        sequence += sum(1 for line in events_path.read_text().splitlines() if line.strip())
    event = {"sequence": sequence, **event}
    with events_path.open("a", encoding="utf-8") as events:
        events.write(json.dumps(event, sort_keys=True) + "\n")
    return event
