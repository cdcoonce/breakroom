from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

from breakroom import jsonio, norms
from breakroom.events import append_event
from breakroom.narrator import render_scene

# `norm_tags` here is authored vocabulary for the incident type. It is NOT the engine's
# output — the emitted event carries that separately as a top-level key. `needs_cleanup`
# decides whether the incident leaves a physical mess someone owns clearing.
INCIDENTS = [
    {
        "id": "coffee-spill",
        "name": "Coffee Spill",
        "room": "break-room",
        "morale_delta": -2,
        "norm_tags": ["care", "shared-space"],
        "needs_cleanup": True,
    },
    {
        "id": "printer-jam",
        "name": "Printer Jam",
        "room": "open-office",
        "morale_delta": -1,
        "norm_tags": ["duty", "patience"],
        "needs_cleanup": True,
    },
    {
        "id": "awkward-silence",
        "name": "Awkward Silence",
        "room": "break-room",
        "morale_delta": -2,
        "norm_tags": ["belonging", "candor"],
        "needs_cleanup": False,
    },
]

STORYLETS = {
    "coffee-spill": {
        "id": "shared-space-repair",
        "name": "Shared Space Repair",
        "prompt": "A small mess tests whether people treat shared space as shared responsibility.",
    },
    "printer-jam": {
        "id": "stuck-workflow",
        "name": "Stuck Workflow",
        "prompt": "A blocked tool turns ordinary patience into visible labor.",
    },
    "awkward-silence": {
        "id": "quiet-room",
        "name": "Quiet Room",
        "prompt": "A room goes quiet, and someone has to decide whether to bridge the gap.",
    },
}


def tick_world(world: Path) -> None:
    state_path = world / "state" / "tower.json"
    state = json.loads(state_path.read_text())
    day = state["day"] + 1
    incident = select_incident(seed=state["seed"], day=day)
    storylet = STORYLETS[incident["id"]]

    # The cleanup owner is the acting character, so the cast has to be resolved before
    # the incident event is emitted.
    character = load_character(world, state["characters"][0])

    # A copy: INCIDENTS is module-level shared state and the raw entry stays raw, so the
    # room lookup, the brief, and the chronicle keep reading it unwidened. `resolved` is
    # a literal False — nothing in the system can clean anything up until the decisions
    # engine (#13) lands.
    emitted_incident = {
        **incident,
        "cleanup_owner": character["id"] if incident["needs_cleanup"] else None,
        "resolved": False,
    }
    incident_event: dict[str, Any] = {
        "type": "incident",
        "day": day,
        "incident": emitted_incident,
        "storylet": storylet,
    }
    registry = _load_registry(world)
    norm_violations: list[dict[str, Any]] = []
    if registry is not None:
        tags = norms.tag_record(registry, incident_event)
        norm_violations = tags["norm_violations"]
        incident_event.update(tags)
    append_event(world, incident_event)

    room = next(room for room in state["rooms"] if room["id"] == incident["room"])
    brief = {
        "day": day,
        "character": character,
        "room": room,
        "incident": incident,
        "storylet": storylet,
        "state": {
            "budget": state["budget"],
            "morale": state["morale"],
            "reputation": state["reputation"],
        },
    }
    prose = render_scene(brief)
    scene_event: dict[str, Any] = {
        "type": "scene",
        "day": day,
        "character_id": character["id"],
        "incident_id": incident["id"],
        "brief": brief,
        "prose": prose,
    }
    if registry is not None:
        scene_event["integrity_drift"] = norms.integrity_drift(
            character, registry, norm_violations
        )
    append_event(world, scene_event)

    state["day"] = day
    state["morale"] += incident["morale_delta"]
    jsonio.write_pretty_json(state_path, state)
    write_chronicle(world, day=day, brief=brief, prose=prose)


def _load_registry(world: Path) -> norms.Registry | None:
    """The registry is optional: a world initialized before norms existed still ticks.

    Mirrors the graceful skip in `secrets._tag_with_norms`. Scaffolding the file is
    `init_world`'s job, never the tick loop's.
    """
    if not (world / "data" / "norms.toml").exists():
        return None
    return norms.load_registry(world)


def select_incident(seed: int, day: int) -> dict[str, Any]:
    digest = hashlib.sha256(f"{seed}:{day}:incident".encode()).hexdigest()
    return INCIDENTS[int(digest, 16) % len(INCIDENTS)]


def load_character(world: Path, character_id: str) -> dict[str, Any]:
    character_path = world / "characters" / f"{character_id}.toml"
    character = tomllib.loads(character_path.read_text())
    return {"id": character_id, **character}


def write_chronicle(world: Path, day: int, brief: dict[str, Any], prose: str) -> None:
    chronicle = world / "chronicles" / f"day-{day:04d}.md"
    chronicle.write_text(
        f"# Day {day:04d}\n\n{prose}\n\n"
        "## Trace\n\n"
        f"brief:\n```json\n{json.dumps(brief, indent=2, sort_keys=True)}\n```\n",
        encoding="utf-8",
    )
