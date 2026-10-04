from __future__ import annotations

import copy
import json
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from breakroom import jsonio


class ValidationError(ValueError):
    pass


_CHARACTER_QUALITY_NAMESPACES = {"trait", "state", "skill", "value", "role"}
_LEGACY_EDGE_KEY_ENCODING = "legacy-v0"
_EDGE_KEY_ENCODING = "json-pair-v1"


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
    next_state = _normalize_edge_state(state, context="reducer state")
    event_type = event["type"]
    if event_type == "incident":
        incident = event.get("incident")
        if not isinstance(incident, dict):
            raise ValidationError("incident event: incident must be an object")
        next_state["day"] = max(next_state["day"], event["day"])
        from breakroom.economy import move_dial

        next_state = move_dial(
            next_state,
            event,
            legacy_unclamped="dial_movement" not in event,
        )
    elif event_type == "scene":
        next_state["day"] = max(next_state["day"], event["day"])
        _apply_scene_spotlight(next_state, event)
    elif event_type == "dial_delta":
        next_state["day"] = max(next_state["day"], event["day"])
        from breakroom.economy import move_dial

        next_state = move_dial(
            next_state,
            event,
            legacy_unclamped="dial_movement" not in event,
        )
    elif event_type == "edge_delta":
        next_state["day"] = max(next_state["day"], event["day"])
        _apply_edge_delta(next_state, event)
    elif event_type == "quiet_day":
        next_state["day"] = max(next_state["day"], event["day"])
    elif event_type in {
        "contract_offer",
        "contract_accepted",
        "contract_declined",
        "contract_expired",
        "contract_progress",
        "contract_pressure",
        "contract_completed",
        "contract_missed",
    }:
        _apply_contract_event(next_state, event)
    else:
        raise ValidationError(f"event type unsupported: {event_type}")
    return next_state


def edge_qualities(state: dict[str, Any], from_id: str, to_id: str) -> dict[str, Any]:
    _validate_edge_keys(state, context="edge_qualities state")
    pair = state.get("edges", {}).get(_pair_key_for_state(state, from_id, to_id), {})
    return copy.deepcopy(pair)


def character_qualities(characters: dict[str, dict[str, Any]], character_id: str) -> dict[str, Any]:
    qualities = characters.get(character_id, {}).get("qualities", {})
    return copy.deepcopy(qualities)


def character_edges(state: dict[str, Any], character_id: str) -> list[dict[str, Any]]:
    _validate_edge_keys(state, context="character_edges state")
    results = []
    for pair_key, qualities in state.get("edges", {}).items():
        from_id, to_id = _split_pair_key_for_state(state, pair_key, context="character_edges state")
        if from_id == character_id or to_id == character_id:
            results.append({"from": from_id, "to": to_id, "qualities": copy.deepcopy(qualities)})
    return results


def edges_at_or_above(state: dict[str, Any], quality: str, threshold: int) -> list[dict[str, Any]]:
    _validate_edge_keys(state, context="edges_at_or_above state")
    results = []
    for pair_key, qualities in state.get("edges", {}).items():
        entry = qualities.get(quality)
        if entry is not None and entry["value"] >= threshold:
            from_id, to_id = _split_pair_key_for_state(
                state, pair_key, context="edges_at_or_above state"
            )
            results.append({"from": from_id, "to": to_id, "value": entry["value"]})
    return results


def edge_provenance(state: dict[str, Any], from_id: str, to_id: str, quality: str) -> list[str]:
    _validate_edge_keys(state, context="edge_provenance state")
    pair = state.get("edges", {}).get(_pair_key_for_state(state, from_id, to_id), {})
    entry = pair.get(quality)
    if entry is None:
        return []
    return [change["event_id"] for change in entry["history"]]


def ticks_since_spotlight(state: dict[str, Any], character_id: str, current_day: int) -> int | None:
    last_day = state.get("spotlight_history", {}).get(character_id)
    if last_day is None:
        return None
    return current_day - last_day


def _edge_encoding(state: dict[str, Any], *, context: str) -> str:
    if "edge_key_encoding" not in state:
        return _LEGACY_EDGE_KEY_ENCODING
    encoding = state["edge_key_encoding"]
    if encoding != _EDGE_KEY_ENCODING:
        raise ValidationError(
            f"{context}: edge_key_encoding: unsupported encoding {encoding!r}"
        )
    return encoding


def _pair_key(from_id: str, to_id: str) -> str:
    if not isinstance(from_id, str) or not isinstance(to_id, str):
        raise ValidationError("edge endpoints must be strings")
    return json.dumps([from_id, to_id], ensure_ascii=True, separators=(",", ":"))


def _pair_key_for_state(state: dict[str, Any], from_id: str, to_id: str) -> str:
    if _edge_encoding(state, context="edge state") == _LEGACY_EDGE_KEY_ENCODING:
        if not isinstance(from_id, str) or not isinstance(to_id, str):
            raise ValidationError("edge endpoints must be strings")
        return f"{from_id}->{to_id}"
    return _pair_key(from_id, to_id)


def _split_pair_key(pair_key: str) -> tuple[str, str]:
    from_id, to_id = pair_key.split("->", 1)
    return from_id, to_id


def _split_pair_key_for_state(
    state: dict[str, Any], pair_key: str, *, context: str
) -> tuple[str, str]:
    encoding = _edge_encoding(state, context=context)
    if encoding == _LEGACY_EDGE_KEY_ENCODING:
        try:
            return _split_pair_key(pair_key)
        except (AttributeError, ValueError) as exc:
            raise ValidationError(f"{context}: invalid legacy edge key {pair_key!r}") from exc

    try:
        pair = json.loads(pair_key)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{context}: invalid json-pair-v1 edge key {pair_key!r}") from exc
    if (
        not isinstance(pair, list)
        or len(pair) != 2
        or not all(isinstance(part, str) for part in pair)
        or _pair_key(pair[0], pair[1]) != pair_key
    ):
        raise ValidationError(
            f"{context}: json-pair-v1 edge key is not a canonical string pair: {pair_key!r}"
        )
    return pair[0], pair[1]


def _validate_edge_keys(state: dict[str, Any], *, context: str) -> None:
    if _edge_encoding(state, context=context) != _EDGE_KEY_ENCODING:
        return
    edges = state.get("edges", {})
    if not isinstance(edges, dict):
        raise ValidationError(f"{context}: edges must be an object")
    for pair_key in edges:
        _split_pair_key_for_state(state, pair_key, context=context)


def _normalize_edge_state(state: dict[str, Any], *, context: str) -> dict[str, Any]:
    normalized = copy.deepcopy(state)
    _edge_encoding(normalized, context=context)
    edges = normalized.get("edges", {})
    if not isinstance(edges, dict):
        raise ValidationError(f"{context}: edges must be an object")
    migrated: dict[str, Any] = {}
    for key, value in edges.items():
        from_id, to_id = _split_pair_key_for_state(normalized, key, context=context)
        migrated[_pair_key(from_id, to_id)] = value
    if "edges" in normalized:
        normalized["edges"] = migrated
    normalized["edge_key_encoding"] = _EDGE_KEY_ENCODING
    return normalized


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
    storylet_history = state.setdefault("storylet_history", {})
    storylet_history[event["storylet_id"]] = event["day"]


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
        entry["history"].append(
            {"event_id": event_id, "delta": delta, "cap": effective_cap, "floor": effective_floor}
        )


def _apply_contract_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    """Replay one frozen contract transition without interpreting current configuration."""
    event_type = event["type"]
    records = state.setdefault("contracts", {})
    if not isinstance(records, dict):
        raise ValidationError("contract event: state contracts must be an object")
    if event_type == "contract_offer":
        offer_id = event.get("offer_id")
        if not isinstance(offer_id, str) or not offer_id or offer_id in records:
            raise ValidationError("contract_offer event: invalid or duplicate offer_id")
        terms = event.get("terms")
        if not isinstance(terms, dict):
            raise ValidationError("contract_offer event: terms must be an object")
        record = copy.deepcopy(event)
        record.pop("sequence", None)
        record["status"] = "offered"
        record["progress"] = 0
        record["emitted_pressure"] = []
        records[offer_id] = record
        return

    contract_id = (
        event.get("offer_id")
        if event_type == "contract_expired"
        else event.get("contract_id")
    )
    record = records.get(contract_id) if isinstance(contract_id, str) else None
    if not isinstance(record, dict):
        raise ValidationError(f"{event_type} event: unknown contract {contract_id!r}")
    if event_type == "contract_accepted":
        if record.get("status") != "offered":
            raise ValidationError(f"contract {contract_id!r} is not available for acceptance")
        team_ids = event.get("team_ids")
        terms = event.get("terms")
        work_room_id = event.get("work_room_id")
        if (
            not isinstance(team_ids, list)
            or not team_ids
            or not all(isinstance(member, str) for member in team_ids)
            or not isinstance(work_room_id, str)
            or not isinstance(terms, dict)
        ):
            raise ValidationError("contract_accepted event: invalid team, room, or terms")
        if len(set(team_ids)) != len(team_ids):
            raise ValidationError("contract_accepted event: duplicate team member")
        record.update(
            status="accepted",
            accepted_day=event.get("day"),
            deadline_day=event.get("day") + terms["duration_ticks"],
            team_ids=copy.deepcopy(team_ids),
            work_room_id=work_room_id,
            terms=copy.deepcopy(terms),
        )
    elif event_type in {"contract_declined", "contract_expired"}:
        if record.get("status") != "offered":
            raise ValidationError(f"contract {contract_id!r} is not an open offer")
        record["status"] = "declined" if event_type == "contract_declined" else "expired"
    elif event_type == "contract_progress":
        if record.get("status") != "accepted":
            raise ValidationError(f"contract {contract_id!r} is not active")
        delta = event.get("work_delta")
        progress = event.get("progress")
        if (
            isinstance(delta, bool)
            or not isinstance(delta, (int, float))
            or isinstance(delta, float) and not math.isfinite(delta)
            or delta < 0
            or isinstance(progress, bool)
            or not isinstance(progress, (int, float))
            or isinstance(progress, float) and not math.isfinite(progress)
            or progress < 0
        ):
            raise ValidationError(
                "contract_progress event: work values must be finite nonnegative numbers"
            )
        record["progress"] = progress
        record["last_work"] = {
            key: copy.deepcopy(event[key])
            for key in (
                "day", "work_delta", "progress", "team_focus", "work_room_id",
                "work_room_kind", "required_room_kind", "fit_factor"
            )
            if key in event
        }
    elif event_type == "contract_pressure":
        if record.get("status") != "accepted":
            raise ValidationError(f"contract {contract_id!r} is not active")
        level = event.get("level")
        emitted = record.setdefault("emitted_pressure", [])
        if not isinstance(level, str) or not level or level in emitted:
            raise ValidationError(f"contract {contract_id!r}: duplicate or invalid pressure level")
        emitted.append(level)
    elif event_type in {"contract_completed", "contract_missed"}:
        if record.get("status") != "accepted":
            raise ValidationError(f"contract {contract_id!r} is not active")
        record["status"] = "completed" if event_type == "contract_completed" else "missed"
    else:
        raise ValidationError(f"event type unsupported: {event_type}")


def replay_events(initial_state: dict[str, Any], events_path: Path) -> dict[str, Any]:
    state = _normalize_edge_state(initial_state, context="replay initial state")
    state.setdefault("contracts", {})
    for line_number, line in enumerate(events_path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            checkout_root = Path(__file__).resolve().parents[2]
            relative_path = events_path.resolve().relative_to(checkout_root, walk_up=True)
            raise ValidationError(
                f"{relative_path}: line {line_number}: invalid JSON"
            ) from exc
        state = apply_event(state, event)
    return state


def write_snapshot(world: Path, state: dict[str, Any], name: str) -> Path:
    if "/" in name or "\\" in name or name == "..":
        raise ValidationError(f"invalid snapshot name: {name!r}")
    snapshots = world / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    path = snapshots / f"{name}.json"
    jsonio.write_pretty_json(path, _normalize_edge_state(state, context="snapshot write"))
    return path


def load_snapshot(path: Path) -> dict[str, Any]:
    state = _read_json(path)
    _validate_edge_keys(state, context=f"snapshot {path}")
    return state


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
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        repository_root = Path(__file__).resolve().parents[2]
        relative = path.resolve().relative_to(repository_root, walk_up=True)
        raise ValidationError(f"{relative}: invalid JSON: {exc}") from exc


def _validate_tower(state: dict[str, Any]) -> None:
    _validate_edge_keys(state, context="state/tower.json")
    for field in ("seed", "day", "budget", "morale", "reputation", "rooms", "characters"):
        if field not in state:
            raise ValidationError(f"state/tower.json: missing {field}")
    state.setdefault("contracts", {})
    if not isinstance(state["contracts"], dict):
        raise ValidationError("state/tower.json: contracts must be an object")
    for field in ("seed", "day"):
        value = state[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"state/tower.json: {field} must be an int")
    for field in ("budget", "morale", "reputation"):
        value = state[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            raise ValidationError(f"state/tower.json: {field} must be a finite int or float")
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
    if character["id"] != character_id:
        raise ValidationError(
            f"{relative}: id {character['id']!r} does not match {character_id!r}"
        )
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
