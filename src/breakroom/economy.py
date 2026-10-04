"""Versioned dial movement and its world-specific rulebook."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any

from breakroom.resolution.rng import RngStream, RollLog
from breakroom.worldstate import ValidationError

_DIALS = {"budget", "morale", "reputation"}
_MOVEMENT_KEY = "dial_movement"
_PAYROLL_RATE_KEY = "per_character_rate"
_THRESHOLD_ID = re.compile(r"[a-z][a-z0-9_]*", re.ASCII)
_THRESHOLD_DIALS = {"budget", "morale"}
_CONTRACT_ROOT_KEYS = {
    "offer_probability_per_reputation_point",
    "offer_lifetime_ticks",
    "matching_room_factor",
    "mismatching_room_factor",
    "templates",
}
_CONTRACT_TEMPLATE_KEYS = {
    "client",
    "required_work_units",
    "duration_ticks",
    "required_room_kind",
    "payout_budget",
    "miss_penalty_budget",
    "miss_penalty_reputation",
    "pressure_milestones",
}


def load_contract_config(world: Path) -> dict[str, Any]:
    """Load and validate the complete contract configuration for a world."""
    override = world / "data" / "contracts.toml"
    if _path_entry_exists(override):
        if not override.is_file():
            raise ValidationError(f"{override}: contract configuration must be a regular file")
        try:
            text = override.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValidationError(f"{override}: cannot read contract configuration: {exc}") from exc
        return _parse_contract_config(text, str(override))
    try:
        text = files("breakroom").joinpath("data/contracts.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValidationError(f"bundled contract configuration: cannot read: {exc}") from exc
    return _parse_contract_config(text, "bundled contract configuration")


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValidationError(f"{path}: cannot inspect contract configuration: {exc}") from exc
    return True


def _parse_contract_config(text: str, context: str) -> dict[str, Any]:
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"{context}: invalid TOML: {exc}") from exc
    if not isinstance(config, dict) or set(config) != _CONTRACT_ROOT_KEYS:
        raise ValidationError(f"{context}: expected offer settings and templates only")
    rate = _finite_number(
        config["offer_probability_per_reputation_point"],
        f"{context}: offer_probability_per_reputation_point",
    )
    if not 0 <= rate <= 0.01:
        raise ValidationError(f"{context}: offer rate must be between 0 and 0.01")
    lifetime = config["offer_lifetime_ticks"]
    if isinstance(lifetime, bool) or not isinstance(lifetime, int) or lifetime <= 0:
        raise ValidationError(f"{context}: offer_lifetime_ticks must be a positive integer")
    matching = _finite_number(config["matching_room_factor"], f"{context}: matching_room_factor")
    mismatch = _finite_number(
        config["mismatching_room_factor"], f"{context}: mismatching_room_factor"
    )
    if matching != 1.0 or not 0 <= mismatch <= 1:
        raise ValidationError(
            f"{context}: room factors must be within [0, 1], with match fixed at 1.0"
        )
    raw_templates = config["templates"]
    if not isinstance(raw_templates, dict) or set(raw_templates) != {"standard"}:
        raise ValidationError(f"{context}: templates must contain exactly standard")
    templates: dict[str, Any] = {}
    for template_id, raw in raw_templates.items():
        templates[template_id] = _validate_contract_template(raw, context)
    return {
        "offer_probability_per_reputation_point": rate,
        "offer_lifetime_ticks": lifetime,
        "matching_room_factor": matching,
        "mismatching_room_factor": mismatch,
        "templates": templates,
    }


def _validate_contract_template(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _CONTRACT_TEMPLATE_KEYS:
        raise ValidationError(f"{context}: standard template has missing or unsupported fields")
    for field in ("client", "required_room_kind"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValidationError(f"{context}: standard {field} must be a nonempty string")
    for field in ("duration_ticks",):
        item = value[field]
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise ValidationError(f"{context}: standard {field} must be a positive integer")
    work = _finite_number(value["required_work_units"], f"{context}: required_work_units")
    if work <= 0:
        raise ValidationError(f"{context}: required_work_units must be positive")
    for field in ("payout_budget", "miss_penalty_budget", "miss_penalty_reputation"):
        amount = _finite_number(value[field], f"{context}: {field}")
        if amount < 0:
            raise ValidationError(f"{context}: {field} must be nonnegative")
    milestones = value["pressure_milestones"]
    if not isinstance(milestones, list):
        raise ValidationError(f"{context}: pressure_milestones must be an array")
    seen_days: set[int] = set()
    seen_levels: set[str] = set()
    previous = value["duration_ticks"]
    normalized = []
    for item in milestones:
        if not isinstance(item, dict) or set(item) != {"ticks_remaining", "level"}:
            raise ValidationError(
                f"{context}: each pressure milestone needs ticks_remaining and level"
            )
        remaining, level = item["ticks_remaining"], item["level"]
        if (
            isinstance(remaining, bool)
            or not isinstance(remaining, int)
            or not 0 <= remaining < value["duration_ticks"]
            or remaining in seen_days
            or remaining >= previous
        ):
            raise ValidationError(f"{context}: pressure thresholds must be unique and descending")
        if not isinstance(level, str) or not level.strip() or level in seen_levels:
            raise ValidationError(f"{context}: pressure levels must be nonempty and unique")
        previous = remaining
        seen_days.add(remaining)
        seen_levels.add(level)
        normalized.append({"ticks_remaining": remaining, "level": level})
    return {**value, "pressure_milestones": normalized}


def list_contracts(world: Path) -> list[dict[str, Any]]:
    """Return copied offer and contract records in stable ID order."""
    from breakroom.worldstate import load_world

    state = load_world(world).state
    records = state.get("contracts", {})
    if not isinstance(records, dict):
        raise ValidationError("state/tower.json: contracts must be an object")
    return [copy.deepcopy(records[key]) for key in sorted(records)]


def accept_contract(
    world: Path, offer_id: str, team_ids: list[str], work_room_id: str | None = None
) -> dict[str, Any]:
    from breakroom import jsonio, worldstate
    from breakroom.events import append_event

    loaded = worldstate.load_world(world)
    state = loaded.state
    offer = _require_offer(state, offer_id)
    day = state["day"]
    if day >= offer["expires_day"]:
        raise ValidationError(f"offer {offer_id!r} expired on day {offer['expires_day']}")
    if not isinstance(team_ids, list) or not team_ids:
        raise ValidationError("contract team must be a nonempty list of character IDs")
    if any(not isinstance(member, str) for member in team_ids):
        raise ValidationError("contract team IDs must be strings")
    if len(set(team_ids)) != len(team_ids):
        raise ValidationError("contract team contains duplicate character IDs")
    for member in team_ids:
        if member not in loaded.characters:
            raise ValidationError(f"contract team references unknown character {member!r}")
        _validated_focus(loaded.characters[member], member)
    if work_room_id is None:
        matching = [
            room
            for room in state["rooms"]
            if room.get("kind") == offer["terms"]["required_room_kind"]
        ]
        if not matching:
            raise ValidationError(
                f"no room matches required kind {offer['terms']['required_room_kind']!r}"
            )
        room = matching[0]
        work_room_id = room["id"]
    else:
        room = next((room for room in state["rooms"] if room.get("id") == work_room_id), None)
        if room is None:
            raise ValidationError(f"unknown work room {work_room_id!r}")
    event = {
        "type": "contract_accepted",
        "day": day,
        "contract_id": offer_id,
        "team_ids": list(team_ids),
        "work_room_id": work_room_id,
        "terms": copy.deepcopy(offer["terms"]),
    }
    staged = worldstate.apply_event(state, event)
    append_event(world, event)
    normalized = worldstate._normalize_edge_state(staged, context="state/tower.json")
    jsonio.write_pretty_json(world / "state" / "tower.json", normalized)
    return copy.deepcopy(staged["contracts"][offer_id])


def decline_contract(world: Path, offer_id: str) -> dict[str, Any]:
    from breakroom import jsonio, worldstate
    from breakroom.events import append_event

    state = worldstate.load_world(world).state
    offer = _require_offer(state, offer_id)
    if state["day"] >= offer["expires_day"]:
        raise ValidationError(f"offer {offer_id!r} expired on day {offer['expires_day']}")
    event = {"type": "contract_declined", "day": state["day"], "contract_id": offer_id}
    staged = worldstate.apply_event(state, event)
    append_event(world, event)
    normalized = worldstate._normalize_edge_state(staged, context="state/tower.json")
    jsonio.write_pretty_json(world / "state" / "tower.json", normalized)
    return copy.deepcopy(staged["contracts"][offer_id])


def _require_offer(state: dict[str, Any], offer_id: str) -> dict[str, Any]:
    if not isinstance(offer_id, str):
        raise ValidationError("offer ID must be a string")
    records = state.get("contracts", {})
    offer = records.get(offer_id) if isinstance(records, dict) else None
    if not isinstance(offer, dict):
        raise ValidationError(f"unknown offer {offer_id!r}")
    if offer.get("status") != "offered":
        raise ValidationError(f"offer {offer_id!r} is {offer.get('status')!r}, not offered")
    return offer


def _validated_focus(character: dict[str, Any], character_id: str) -> int:
    focus = character.get("stats", {}).get("focus")
    if isinstance(focus, bool) or not isinstance(focus, int) or focus < 0:
        raise ValidationError(f"character {character_id!r}: focus must be a nonnegative integer")
    return focus


def contract_tick(
    state: dict[str, Any],
    characters: dict[str, dict[str, Any]],
    config: dict[str, Any],
    *,
    day: int,
    rulebook: dict[str, Any],
    log: RollLog,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Resolve deterministic contract events against staged tick state."""
    from breakroom import worldstate

    events: list[dict[str, Any]] = []
    records = state.setdefault("contracts", {})
    for contract_id in sorted(records):
        record = records[contract_id]
        if record.get("status") == "offered" and day >= record["expires_day"]:
            event = {"type": "contract_expired", "day": day, "offer_id": contract_id}
            state = worldstate.apply_event(state, event)
            events.append(event)
    probability = min(100, max(0, state["reputation"])) * config[
        "offer_probability_per_reputation_point"
    ]
    offered = RngStream(seed=state["seed"], stream="contract_offers", tick=day, log=log).bernoulli(
        "contract_offer", probability=probability
    )
    if offered:
        template = copy.deepcopy(config["templates"]["standard"])
        template["matching_room_factor"] = config["matching_room_factor"]
        template["mismatching_room_factor"] = config["mismatching_room_factor"]
        offer_id = f"contract-offer-{day:04d}"
        event = {
            "type": "contract_offer",
            "day": day,
            "offer_id": offer_id,
            "created_day": day,
            "expires_day": day + config["offer_lifetime_ticks"],
            "client": template["client"],
            "terms": template,
        }
        state = worldstate.apply_event(state, event)
        events.append(event)
    active_ids = sorted(
        key for key, record in state["contracts"].items() if record.get("status") == "accepted"
    )
    for contract_id in active_ids:
        contract = state["contracts"][contract_id]
        team_focus = {
            member: _validated_focus(characters[member], member)
            for member in contract["team_ids"]
        }
        room = next(
            (room for room in state["rooms"] if room.get("id") == contract["work_room_id"]), None
        )
        if room is None:
            raise ValidationError(f"contract {contract_id!r}: assigned work room is missing")
        required_kind = contract["terms"]["required_room_kind"]
        factor = contract["terms"][
            "matching_room_factor" if room["kind"] == required_kind else "mismatching_room_factor"
        ]
        try:
            work = sum(team_focus.values()) * factor
            progress = contract["progress"] + work
        except OverflowError as exc:
            raise ValidationError(
                f"contract {contract_id!r}: work progress must remain finite"
            ) from exc
        if not _contract_number_is_finite(work) or not _contract_number_is_finite(progress):
            raise ValidationError(f"contract {contract_id!r}: work progress must remain finite")
        progress_event = {
            "type": "contract_progress",
            "day": day,
            "contract_id": contract_id,
            "work_delta": work,
            "progress": progress,
            "team_focus": team_focus,
            "work_room_id": room["id"],
            "work_room_kind": room["kind"],
            "required_room_kind": required_kind,
            "fit_factor": factor,
        }
        state = worldstate.apply_event(state, progress_event)
        events.append(progress_event)
        if progress >= contract["terms"]["required_work_units"]:
            settlement = resolve_dial_movement(
                {
                    "type": "dial_delta",
                    "day": day,
                    "dials": {"budget": contract["terms"]["payout_budget"]},
                    "source": "contract_completion",
                    "contract_id": contract_id,
                    "amount": contract["terms"]["payout_budget"],
                    "terms": copy.deepcopy(contract["terms"]),
                },
                rulebook,
            )
            state = move_dial(state, settlement, rulebook=rulebook)
            events.append(settlement)
            terminal = {"type": "contract_completed", "day": day, "contract_id": contract_id}
            state = worldstate.apply_event(state, terminal)
            events.append(terminal)
            continue
        remaining = contract["deadline_day"] - day
        emitted = set(contract.get("emitted_pressure", []))
        for milestone in contract["terms"]["pressure_milestones"]:
            level = milestone["level"]
            if milestone["ticks_remaining"] == remaining and level not in emitted:
                pressure = {
                    "type": "contract_pressure",
                    "day": day,
                    "contract_id": contract_id,
                    "ticks_remaining": remaining,
                    "level": level,
                }
                state = worldstate.apply_event(state, pressure)
                events.append(pressure)
        if day >= contract["deadline_day"]:
            terms = contract["terms"]
            settlement = resolve_dial_movement(
                {
                    "type": "dial_delta",
                    "day": day,
                    "dials": {
                        "budget": -terms["miss_penalty_budget"],
                        "reputation": -terms["miss_penalty_reputation"],
                    },
                    "source": "contract_miss",
                    "contract_id": contract_id,
                    "amount": {
                        "budget": -terms["miss_penalty_budget"],
                        "reputation": -terms["miss_penalty_reputation"],
                    },
                    "terms": copy.deepcopy(terms),
                },
                rulebook,
            )
            state = move_dial(state, settlement, rulebook=rulebook)
            events.append(settlement)
            terminal = {"type": "contract_missed", "day": day, "contract_id": contract_id}
            state = worldstate.apply_event(state, terminal)
            events.append(terminal)
    return state, events


def _contract_number_is_finite(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and (not isinstance(value, float) or math.isfinite(value))
    )


@dataclass(frozen=True)
class ThresholdDefinition:
    """One low-is-bad condition threshold and its re-arm boundary."""

    dial: str
    trip: int | float
    rearm: int | float


class ThresholdRegistry(Mapping[str, ThresholdDefinition]):
    """Read-only threshold definitions keyed by their condition IDs."""

    def __init__(self, definitions: Mapping[str, ThresholdDefinition]) -> None:
        self._definitions = MappingProxyType(dict(definitions))

    def __getitem__(self, key: str) -> ThresholdDefinition:
        return self._definitions[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._definitions)

    def __len__(self) -> int:
        return len(self._definitions)


def load_thresholds(world: Path) -> ThresholdRegistry:
    """Load a complete world threshold registry, or use the bundled defaults."""
    directory = world / "data" / "thresholds"
    if not directory.exists() and not directory.is_symlink():
        return _load_bundled_thresholds()
    if not directory.is_dir():
        raise ValidationError(f"{directory}: threshold configuration must be a directory")

    definitions: dict[str, ThresholdDefinition] = {}
    try:
        children = sorted(directory.iterdir(), key=lambda child: child.name)
    except OSError as exc:
        raise ValidationError(f"{directory}: cannot list threshold configuration: {exc}") from exc
    if not children:
        raise ValidationError(f"{directory}: threshold registry cannot be empty")
    for path in children:
        threshold_id = _threshold_id(path.name, str(path))
        if threshold_id in definitions:
            raise ValidationError(f"{path}: duplicate threshold ID {threshold_id!r}")
        if not path.is_file():
            raise ValidationError(f"{path}: threshold entries must be regular TOML files")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValidationError(f"{path}: cannot read threshold definition: {exc}") from exc
        definitions[threshold_id] = _parse_threshold(text, threshold_id, str(path))
    return ThresholdRegistry(definitions)


def _load_bundled_thresholds() -> ThresholdRegistry:
    resource_dir = files("breakroom").joinpath("data/thresholds")
    try:
        children = sorted(resource_dir.iterdir(), key=lambda child: child.name)
    except (OSError, FileNotFoundError) as exc:
        raise ValidationError(f"bundled thresholds: cannot list definitions: {exc}") from exc
    if not children:
        raise ValidationError("bundled thresholds: registry cannot be empty")
    definitions: dict[str, ThresholdDefinition] = {}
    for resource in children:
        threshold_id = _threshold_id(resource.name, f"bundled threshold {resource.name}")
        if threshold_id in definitions:
            raise ValidationError(f"bundled thresholds: duplicate ID {threshold_id!r}")
        try:
            if not resource.is_file():
                raise ValidationError(
                    f"bundled threshold {resource.name}: expected a regular TOML file"
                )
            text = resource.read_text(encoding="utf-8")
        except ValidationError:
            raise
        except (OSError, UnicodeError) as exc:
            raise ValidationError(
                f"bundled threshold {resource.name}: cannot read: {exc}"
            ) from exc
        definitions[threshold_id] = _parse_threshold(
            text, threshold_id, f"bundled threshold {resource.name}"
        )
    return ThresholdRegistry(definitions)


def _threshold_id(filename: str, context: str) -> str:
    if not filename.endswith(".toml") or filename.count(".") != 1:
        raise ValidationError(f"{context}: filename must have exact lowercase .toml suffix")
    threshold_id = filename[:-5]
    if not _THRESHOLD_ID.fullmatch(threshold_id):
        raise ValidationError(f"{context}: threshold ID must match [a-z][a-z0-9_]*")
    return threshold_id


def _parse_threshold(text: str, threshold_id: str, context: str) -> ThresholdDefinition:
    try:
        value = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"{context}: invalid TOML: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {"dial", "trip", "rearm"}:
        raise ValidationError(f"{context}: expected exactly dial, trip, and rearm")
    dial = value["dial"]
    if not isinstance(dial, str) or dial not in _THRESHOLD_DIALS:
        raise ValidationError(f"{context}: dial must be 'morale' or 'budget'")
    trip = _finite_number(value["trip"], f"{context}: trip")
    rearm = _finite_number(value["rearm"], f"{context}: rearm")
    if trip >= rearm:
        raise ValidationError(f"{context}: trip must be less than rearm")
    return ThresholdDefinition(dial=dial, trip=trip, rearm=rearm)


def check_thresholds(
    state: Mapping[str, Any],
    active: frozenset[str],
    *,
    thresholds: ThresholdRegistry | None = None,
) -> tuple[list[dict[str, Any]], frozenset[str]]:
    """Return deterministic condition transitions without mutating inputs."""
    if not isinstance(state, Mapping):
        raise ValidationError("threshold state must be a mapping")
    if not isinstance(active, frozenset) or any(not isinstance(name, str) for name in active):
        raise ValidationError("active thresholds must be a frozenset of strings")

    registry = _load_bundled_thresholds() if thresholds is None else thresholds
    definitions = _validated_threshold_registry(registry)
    unknown_active = active.difference(definitions)
    if unknown_active:
        raise ValidationError(f"active thresholds are not in registry: {sorted(unknown_active)!r}")

    values: dict[str, int | float] = {}
    for threshold_id, definition in definitions.items():
        if definition.dial not in state:
            raise ValidationError(
                f"threshold {threshold_id}: state is missing dial {definition.dial}"
            )
        values[threshold_id] = _finite_number(
            state[definition.dial], f"threshold {threshold_id}: dial {definition.dial}"
        )

    updated = set(active)
    events: list[dict[str, Any]] = []
    for threshold_id in sorted(definitions):
        definition = definitions[threshold_id]
        value = values[threshold_id]
        if threshold_id in active and value >= definition.rearm:
            updated.remove(threshold_id)
            events.append({"type": "condition", "name": threshold_id, "active": False})
        elif threshold_id not in active and value <= definition.trip:
            updated.add(threshold_id)
            events.append({"type": "condition", "name": threshold_id, "active": True})
    if not events:
        return events, active
    return events, frozenset(updated)


def _validated_threshold_registry(value: Any) -> dict[str, ThresholdDefinition]:
    if not isinstance(value, Mapping):
        raise ValidationError("threshold registry must be a mapping")
    definitions: dict[str, ThresholdDefinition] = {}
    for threshold_id, definition in value.items():
        if not isinstance(threshold_id, str) or not _THRESHOLD_ID.fullmatch(threshold_id):
            raise ValidationError(f"invalid threshold ID: {threshold_id!r}")
        if not isinstance(definition, ThresholdDefinition):
            raise ValidationError(f"threshold {threshold_id}: expected ThresholdDefinition")
        dial = definition.dial
        if not isinstance(dial, str) or dial not in _THRESHOLD_DIALS:
            raise ValidationError(f"threshold {threshold_id}: dial must be 'morale' or 'budget'")
        trip = _finite_number(definition.trip, f"threshold {threshold_id}: trip")
        rearm = _finite_number(definition.rearm, f"threshold {threshold_id}: rearm")
        if trip >= rearm:
            raise ValidationError(f"threshold {threshold_id}: trip must be less than rearm")
        definitions[threshold_id] = ThresholdDefinition(dial=dial, trip=trip, rearm=rearm)
    if not definitions:
        raise ValidationError("threshold registry cannot be empty")
    return definitions


def load_payroll_rate(world: Path) -> int | float:
    """Load a validated world payroll override or the bundled default."""
    override = world / "data" / "payroll.toml"
    if override.exists():
        try:
            text = override.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValidationError(f"{override}: cannot read payroll configuration: {exc}") from exc
        context = str(override)
    else:
        try:
            text = files("breakroom").joinpath("data/payroll.toml").read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValidationError(f"bundled payroll configuration: cannot read: {exc}") from exc
        context = "bundled payroll configuration"
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"{context}: invalid TOML: {exc}") from exc
    if not isinstance(config, dict) or set(config) != {_PAYROLL_RATE_KEY}:
        raise ValidationError(f"{context}: expected only a per_character_rate value")
    rate = _finite_number(config[_PAYROLL_RATE_KEY], f"{context}: per_character_rate")
    if rate < 0:
        raise ValidationError(f"{context}: per_character_rate must be nonnegative")
    return rate


def payroll_receipt(
    per_character_rate: int | float, headcount: int, *, day: int
) -> dict[str, Any]:
    """Build the deterministic, provenance-bearing payroll movement receipt."""
    rate = _finite_number(per_character_rate, "payroll per_character_rate")
    if rate < 0:
        raise ValidationError("payroll per_character_rate must be nonnegative")
    if isinstance(headcount, bool) or not isinstance(headcount, int) or headcount < 0:
        raise ValidationError("payroll headcount must be a nonnegative integer")
    if isinstance(day, bool) or not isinstance(day, int) or day < 0:
        raise ValidationError("payroll day must be a nonnegative integer")
    return {
        "type": "dial_delta",
        "day": day,
        "dials": {"budget": -(rate * headcount)},
        "source": "payroll",
        "headcount": headcount,
        "per_character_rate": rate,
    }


def load_rulebook(world: Path) -> dict[str, Any]:
    """Load the world's override, or the immutable packaged default."""
    override = world / "data" / "economy.toml"
    if override.exists():
        try:
            text = override.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValidationError(f"{override}: cannot read economy rulebook: {exc}") from exc
        context = str(override)
    else:
        return _load_bundled_rulebook()
    return _parse_rulebook(text, context)


def _load_bundled_rulebook() -> dict[str, Any]:
    try:
        text = files("breakroom").joinpath("data/economy.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValidationError(f"bundled economy rulebook: cannot read: {exc}") from exc
    return _parse_rulebook(text, "bundled economy rulebook")


def _parse_rulebook(text: str, context: str) -> dict[str, Any]:
    try:
        rulebook = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"{context}: invalid TOML: {exc}") from exc
    _validate_rulebook(rulebook, context)
    return rulebook


def resolve_dial_movement(event: dict[str, Any], rulebook: dict[str, Any]) -> dict[str, Any]:
    """Return a deep-copied event carrying an immutable v1 movement receipt."""
    _validate_rulebook(rulebook, "economy rulebook")
    if not isinstance(event, dict):
        raise ValidationError("dial movement event must be an object")
    result = copy.deepcopy(event)
    if _MOVEMENT_KEY in event:
        raise ValidationError("dial movement event already contains dial_movement metadata")
    event_type = event.get("type")
    events = rulebook["events"]
    if event_type == "incident":
        config = events["incident"]
        incident = event.get("incident")
        if not isinstance(incident, dict):
            raise ValidationError("incident event: incident must be an object")
        amount = _get_path(event, config["amount_path"], default=0)
        dials = {config["dial"]: _finite_number(amount, "incident morale_delta")}
    elif event_type == "dial_delta":
        payload = event.get("dials")
        if not isinstance(payload, dict):
            raise ValidationError("dial_delta event: dials must be an object")
        dials = {}
        for dial, amount in payload.items():
            if dial not in _DIALS:
                raise ValidationError(f"unknown dial: {dial}")
            dials[dial] = _finite_number(amount, f"dial_delta event: {dial} delta")
    else:
        raise ValidationError(f"event type does not move dials: {event_type!r}")
    result[_MOVEMENT_KEY] = {
        "version": 1,
        "rulebook_sha256": _rulebook_digest(rulebook),
        "dials": dials,
    }
    return result


def move_dial(
    state: dict[str, Any],
    event: dict[str, Any],
    *,
    rulebook: dict[str, Any] | None = None,
    legacy_unclamped: bool = False,
) -> dict[str, Any]:
    """Apply an event's dial movement to a copy of state."""
    if not isinstance(state, dict) or not isinstance(event, dict):
        raise ValidationError("dial movement state and event must be objects")
    has_metadata = _MOVEMENT_KEY in event
    if legacy_unclamped:
        if rulebook is not None or has_metadata:
            raise ValidationError(
                "legacy_unclamped conflicts with rulebook or dial_movement metadata"
            )
        result = copy.deepcopy(state)
        event_type = event.get("type")
        if event_type == "incident":
            incident = event.get("incident")
            if not isinstance(incident, dict):
                raise ValidationError("incident event: incident must be an object")
            delta = incident.get("morale_delta", 0)
            if isinstance(delta, bool) or not isinstance(delta, (int, float)):
                raise ValidationError("incident event: morale_delta must be numeric")
            result["morale"] += delta
        elif event_type == "dial_delta":
            dials = event.get("dials")
            if not isinstance(dials, dict):
                raise ValidationError("dial_delta event: dials must be an object")
            for dial, delta in dials.items():
                if dial not in result:
                    raise ValidationError(f"unknown dial: {dial}")
                if isinstance(delta, bool) or not isinstance(delta, (int, float)):
                    raise ValidationError(f"dial_delta event: {dial} delta must be an int or float")
                result[dial] += delta
        else:
            raise ValidationError(f"event type does not move dials: {event_type!r}")
        return result
    if not has_metadata:
        receipt = resolve_dial_movement(
            event, rulebook if rulebook is not None else _load_bundled_rulebook()
        )
        movement = _validate_movement(receipt[_MOVEMENT_KEY])
    else:
        movement = _validate_movement(event[_MOVEMENT_KEY])

    result = copy.deepcopy(state)
    for dial, amount in movement["dials"].items():
        if dial not in result:
            raise ValidationError(f"dial movement: state is missing {dial}")
        current = _finite_number(result[dial], f"dial movement: current {dial}")
        try:
            value = current + amount
        except OverflowError as exc:
            raise ValidationError(f"dial movement: {dial} result must be finite") from exc
        if isinstance(value, float) and not math.isfinite(value):
            raise ValidationError(f"dial movement: {dial} result must be finite")
        if dial in {"morale", "reputation"}:
            value = min(100, max(0, value))
        result[dial] = value
    return result


def _validate_rulebook(rulebook: Any, context: str) -> None:
    if not isinstance(rulebook, dict) or set(rulebook) != {"events"}:
        raise ValidationError(f"{context}: expected only an events table")
    events = rulebook["events"]
    if not isinstance(events, dict) or set(events) != {"incident", "dial_delta"}:
        raise ValidationError(f"{context}: events must define incident and dial_delta")
    incident = events["incident"]
    if not isinstance(incident, dict) or set(incident) != {"dial", "amount_path"}:
        raise ValidationError(f"{context}: invalid events.incident mapping")
    dial = incident["dial"]
    if (
        not isinstance(dial, str)
        or dial not in _DIALS
        or incident["amount_path"] != "incident.morale_delta"
    ):
        raise ValidationError(f"{context}: invalid events.incident mapping")
    dial_delta = events["dial_delta"]
    if not isinstance(dial_delta, dict) or set(dial_delta) != {"dials_path"}:
        raise ValidationError(f"{context}: invalid events.dial_delta mapping")
    if dial_delta.get("dials_path") != "dials":
        raise ValidationError(f"{context}: events.dial_delta.dials_path must be 'dials'")


def _validate_movement(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"version", "rulebook_sha256", "dials"}:
        raise ValidationError(
            "dial_movement metadata must contain version, rulebook_sha256, and dials"
        )
    version = value["version"]
    digest = value["rulebook_sha256"]
    dials = value["dials"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValidationError("dial_movement version must be integer 1")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise ValidationError("dial_movement rulebook_sha256 must be lowercase 64-character hex")
    if not isinstance(dials, dict):
        raise ValidationError("dial_movement dials must be an object")
    checked = {}
    for dial, amount in dials.items():
        if dial not in _DIALS:
            raise ValidationError(f"dial_movement contains unknown dial: {dial}")
        checked[dial] = _finite_number(amount, f"dial_movement {dial}")
    return {"version": 1, "rulebook_sha256": digest, "dials": checked}


def _finite_number(value: Any, context: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{context} must be a finite int or float")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError(f"{context} must be a finite int or float")
    return value


def _get_path(value: dict[str, Any], path: str, *, default: Any) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _rulebook_digest(rulebook: dict[str, Any]) -> str:
    try:
        canonical = json.dumps(
            rulebook,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"economy rulebook is not canonical JSON: {exc}") from exc
    return hashlib.sha256(canonical).hexdigest()
