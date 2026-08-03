from __future__ import annotations

import copy
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class World:
    root: Path
    state: dict[str, Any]
    characters: dict[str, dict[str, Any]]


def load_world(world: Path) -> World:
    state = _read_json(world / "state" / "tower.json")
    _validate_tower(state)
    characters = {
        character_id: _load_character(world, character_id)
        for character_id in state["characters"]
    }
    return World(root=world, state=state, characters=characters)


def apply_event(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    next_state = copy.deepcopy(state)
    event_type = event["type"]
    if event_type == "incident":
        next_state["day"] = max(next_state["day"], event["day"])
        next_state["morale"] += event["incident"].get("morale_delta", 0)
    elif event_type == "scene":
        next_state["day"] = max(next_state["day"], event["day"])
    elif event_type == "dial_delta":
        next_state["day"] = max(next_state["day"], event["day"])
        for dial, delta in event["dials"].items():
            next_state[dial] += delta
    else:
        raise ValidationError(f"event type unsupported: {event_type}")
    return next_state


def replay_events(initial_state: dict[str, Any], events_path: Path) -> dict[str, Any]:
    state = copy.deepcopy(initial_state)
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        state = apply_event(state, json.loads(line))
    return state


def write_snapshot(world: Path, state: dict[str, Any], name: str) -> Path:
    snapshots = world / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    path = snapshots / f"{name}.json"
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_snapshot(path: Path) -> dict[str, Any]:
    return _read_json(path)


def diff_states(left: dict[str, Any], right: dict[str, Any]) -> dict[str, dict[str, Any]]:
    keys = sorted(set(left) | set(right))
    return {
        key: {"left": left.get(key), "right": right.get(key)}
        for key in keys
        if left.get(key) != right.get(key)
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValidationError(f"{path.name}: missing file")
    return json.loads(path.read_text())


def _validate_tower(state: dict[str, Any]) -> None:
    for field in ("seed", "day", "budget", "morale", "reputation", "rooms", "characters"):
        if field not in state:
            raise ValidationError(f"state/tower.json: missing {field}")
    if not isinstance(state["rooms"], list):
        raise ValidationError("state/tower.json: rooms must be a list")
    if not isinstance(state["characters"], list):
        raise ValidationError("state/tower.json: characters must be a list")
    for room in state["rooms"]:
        for field in ("id", "name", "kind", "floor"):
            if field not in room:
                raise ValidationError(f"state/tower.json: room missing {field}")


def _load_character(world: Path, character_id: str) -> dict[str, Any]:
    relative = Path("characters") / f"{character_id}.toml"
    path = world / relative
    if not path.exists():
        raise ValidationError(f"{relative}: missing file")
    character = tomllib.loads(path.read_text())
    for field in ("id", "name", "model", "stats"):
        if field not in character:
            raise ValidationError(f"{relative}: missing {field}")
    for stat in ("focus", "empathy", "nerve"):
        if stat not in character["stats"]:
            raise ValidationError(f"{relative}: missing stats.{stat}")
    return character
