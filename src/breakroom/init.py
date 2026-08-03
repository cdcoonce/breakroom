from __future__ import annotations

import json
from pathlib import Path

STARTER_CHARACTER = """\
id = "jordan-vale"
name = "Jordan Vale"
model = "claude-3-5-haiku"

[stats]
focus = 2
empathy = 3
nerve = 2

qualities = ["new-hire", "people-pleaser"]
declared_values = ["keep the peace", "do competent work"]
"""


def init_world(world: Path, seed: int) -> None:
    (world / "characters").mkdir(parents=True, exist_ok=True)
    (world / "chronicles").mkdir(parents=True, exist_ok=True)
    (world / "state").mkdir(parents=True, exist_ok=True)

    state = {
        "seed": seed,
        "day": 0,
        "budget": 1000,
        "morale": 50,
        "reputation": 50,
        "rooms": [
            {"id": "break-room", "name": "Break Room", "kind": "social", "floor": 1},
            {"id": "open-office", "name": "Open Office", "kind": "work", "floor": 1},
        ],
        "characters": ["jordan-vale"],
    }
    (world / "state" / "tower.json").write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (world / "characters" / "jordan-vale.toml").write_text(
        STARTER_CHARACTER, encoding="utf-8"
    )
    (world / "events.jsonl").write_text("", encoding="utf-8")
