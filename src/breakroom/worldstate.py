from __future__ import annotations

import copy
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from breakroom import jsonio


class ValidationError(ValueError):
    pass


_CHARACTER_QUALITY_NAMESPACES = {"trait", "state", "skill", "value", "role"}


@dataclass(frozen=True)
class World:
    root: Path
    state: dict[str, Any]
    characters: dict[str, dict[str, Any]]


def load_world(world: Path) -> World:
    state = _read_json(world / "state" / "tower.json")
    _validate_tower(state)
    characters = {
        character_id: _load_character(world, character_id) for character_id in state["characters"]
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
        _apply_scene_spotlight(next_state, event)
    elif event_type == "dial_delta":
        next_state["day"] = max(next_state["day"], event["day"])
        for dial, delta in event["dials"].items():
            next_state[dial] += delta
    elif event_type == "edge_delta":
        next_state["day"] = max(next_state["day"], event["day"])
        _apply_edge_delta(next_state, event)
    else:
        raise ValidationError(f"event type unsupported: {event_type}")
    return next_state


def edge_qualities(state: dict[str, Any], from_id: str, to_id: str) -> dict[str, Any]:
    pair = state.get("edges", {}).get(_pair_key(from_id, to_id), {})
    return copy.deepcopy(pair)


def character_qualities(characters: dict[str, dict[str, Any]], character_id: str) -> dict[str, Any]:
    qualities = characters.get(character_id, {}).get("qualities", {})
    return copy.deepcopy(qualities)


def character_edges(state: dict[str, Any], character_id: str) -> list[dict[str, Any]]:
    results = []
    for pair_key, qualities in state.get("edges", {}).items():
        from_id, to_id = _split_pair_key(pair_key)
        if from_id == character_id or to_id == character_id:
            results.append({"from": from_id, "to": to_id, "qualities": copy.deepcopy(qualities)})
    return results


def edges_at_or_above(state: dict[str, Any], quality: str, threshold: int) -> list[dict[str, Any]]:
    results = []
    for pair_key, qualities in state.get("edges", {}).items():
        entry = qualities.get(quality)
        if entry is not None and entry["value"] >= threshold:
            from_id, to_id = _split_pair_key(pair_key)
            results.append({"from": from_id, "to": to_id, "value": entry["value"]})
    return results


def edge_provenance(state: dict[str, Any], from_id: str, to_id: str, quality: str) -> list[str]:
    pair = state.get("edges", {}).get(_pair_key(from_id, to_id), {})
    entry = pair.get(quality)
    if entry is None:
        return []
    return [change["event_id"] for change in entry["history"]]


def ticks_since_spotlight(state: dict[str, Any], character_id: str, current_day: int) -> int | None:
    last_day = state.get("spotlight_history", {}).get(character_id)
    if last_day is None:
        return None
    return current_day - last_day


def _pair_key(from_id: str, to_id: str) -> str:
    return f"{from_id}->{to_id}"


def _split_pair_key(pair_key: str) -> tuple[str, str]:
    from_id, to_id = pair_key.split("->", 1)
    return from_id, to_id


def _apply_scene_spotlight(state: dict[str, Any], event: dict[str, Any]) -> None:
    if "character_ids" not in event:
        return
    character_ids = event["character_ids"]

    if not isinstance(character_ids, list) or not character_ids:
        raise ValidationError("scene event: character_ids must be a non-empty list of strings")
    if not all(isinstance(character_id, str) for character_id in character_ids):
        raise ValidationError("scene event: character_ids must be a non-empty list of strings")
    if "storylet_id" not in event or not isinstance(event["storylet_id"], str):
        raise ValidationError("scene event: character_ids present without storylet_id")

    spotlight_history = state.setdefault("spotlight_history", {})
    for character_id in character_ids:
        spotlight_history[character_id] = event["day"]


def _apply_edge_delta(state: dict[str, Any], event: dict[str, Any]) -> None:
    event_id = event.get("event_id")
    if not event_id:
        raise ValidationError("edge_delta event: missing event_id")
    for field in ("from", "to", "edges"):
        if field not in event:
            raise ValidationError(f"edge_delta event: missing {field}")

    pair_key = _pair_key(event["from"], event["to"])
    edges = state.setdefault("edges", {})
    pair = edges.setdefault(pair_key, {})
    for quality, change in event["edges"].items():
        entry = pair.setdefault(quality, {"value": 0, "cap": None, "floor": None, "history": []})
        delta = change.get("delta", 0)
        cap = change.get("cap")
        floor = change.get("floor")

        effective_cap = entry["cap"]
        if cap is not None:
            effective_cap = cap if effective_cap is None else min(effective_cap, cap)

        effective_floor = entry["floor"]
        if floor is not None:
            effective_floor = floor if effective_floor is None else max(effective_floor, floor)

        if (
            effective_cap is not None
            and effective_floor is not None
            and effective_floor > effective_cap
        ):
            raise ValidationError(
                f"edge_delta event: floor {effective_floor} exceeds cap {effective_cap} "
                f"for {quality}"
            )

        value = entry["value"] + delta
        if effective_cap is not None:
            value = min(value, effective_cap)
        if effective_floor is not None:
            value = max(value, effective_floor)

        entry["value"] = value
        entry["cap"] = effective_cap
        entry["floor"] = effective_floor
        entry["history"].append({"event_id": event_id, "delta": delta, "cap": cap, "floor": floor})


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
    jsonio.write_pretty_json(path, state)
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
    _validate_character_qualities(relative, character.get("qualities", {}))
    return character


def _validate_character_qualities(relative: Path, qualities: Any) -> None:
    if not isinstance(qualities, dict):
        raise ValidationError(f"{relative}: qualities must be a table of namespaced keys")
    for key, value in qualities.items():
        namespace, sep, name = key.partition(":")
        if not sep or not name or namespace not in _CHARACTER_QUALITY_NAMESPACES:
            raise ValidationError(f"{relative}: invalid quality namespace {key!r}")
        if value is True:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{relative}: invalid quality value for {key!r}")
        if value < -3 or value > 3:
            raise ValidationError(f"{relative}: quality {key!r} out of range")
