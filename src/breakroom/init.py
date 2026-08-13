from __future__ import annotations

from pathlib import Path

from breakroom import jsonio
from breakroom.worldstate import ValidationError

# `qualities` and `declared_values` MUST stay above the [stats] table header. TOML binds
# every key after a table header into that table, so listing them below [stats] parsed
# them as stats.declared_values — and every consumer reads character["declared_values"]
# (norms.integrity_drift, docs/design/decision-points.md:121-123), so drift silently
# computed against nothing. The values themselves are #29's territory; only the layout
# is fixed here.
STARTER_CHARACTER = """\
id = "jordan-vale"
name = "Jordan Vale"
model = "claude-3-5-haiku"
qualities = { "state:new-hire" = true, "trait:people-pleaser" = true }
declared_values = ["keep the peace", "do competent work"]

[stats]
focus = 2
empathy = 3
nerve = 2
"""

# Exactly one norm: the only detector reachable from a tracer tick. The other four in
# docs/design/norms.md read record types (decision, location_observation) that nothing
# produces yet, and arrive with the slices that produce them.
#
# Two values are load-bearing and must not drift:
#   related_values — must overlap STARTER_CHARACTER's declared_values, or integrity_drift
#     returns [] and the drift path is vacuously "satisfied". Match the norm to the cast,
#     never the cast to the norm; the founding cast's values belong to #29.
#   detection — the one detector key an incident event can currently trigger.
STARTER_NORMS = """\
[[norms]]
id = "clean-up-after-yourself"
scope = "social_norm"
description = "Whoever leaves a mess in a shared space is the one who clears it."
severity = "minor"
detection = "incident_cleanup_owner_missed"
tags = ["care", "shared-space"]
related_values = ["do competent work"]
"""


# Ported verbatim from tick.py's old hardcoded INCIDENTS table. `base_rate = 1.0` fires
# every incident every tick — probabilistic rates, preconditions, and cascades are the
# engine's job (tested in tests/test_incidents.py), not this starter table's.
STARTER_INCIDENTS = """\
[[incidents]]
id = "coffee-spill"
base_rate = 1.0
rooms = ["break-room"]

  [[incidents.effects]]
  type = "incident_detail"
  name = "Coffee Spill"
  room = "break-room"
  morale_delta = -2
  norm_tags = ["care", "shared-space"]
  needs_cleanup = true

[[incidents]]
id = "printer-jam"
base_rate = 1.0
rooms = ["open-office"]

  [[incidents.effects]]
  type = "incident_detail"
  name = "Printer Jam"
  room = "open-office"
  morale_delta = -1
  norm_tags = ["duty", "patience"]
  needs_cleanup = true

[[incidents]]
id = "awkward-silence"
base_rate = 1.0
rooms = ["break-room"]

  [[incidents.effects]]
  type = "incident_detail"
  name = "Awkward Silence"
  room = "break-room"
  morale_delta = -2
  norm_tags = ["belonging", "candor"]
  needs_cleanup = false
"""


def init_world(world: Path, seed: int) -> None:
    if (world / "state" / "tower.json").exists():
        raise ValidationError(f"{world}: already initialized (state/tower.json exists)")

    (world / "characters").mkdir(parents=True, exist_ok=True)
    (world / "chronicles").mkdir(parents=True, exist_ok=True)
    (world / "state").mkdir(parents=True, exist_ok=True)
    (world / "data").mkdir(parents=True, exist_ok=True)

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
    jsonio.write_pretty_json(world / "state" / "tower.json", state)
    (world / "characters" / "jordan-vale.toml").write_text(
        STARTER_CHARACTER, encoding="utf-8"
    )
    (world / "data" / "norms.toml").write_text(STARTER_NORMS, encoding="utf-8")
    (world / "data" / "incidents.toml").write_text(STARTER_INCIDENTS, encoding="utf-8")
    (world / "events.jsonl").write_text("", encoding="utf-8")
