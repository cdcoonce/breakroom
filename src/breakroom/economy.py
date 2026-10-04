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

from breakroom.worldstate import ValidationError

_DIALS = {"budget", "morale", "reputation"}
_MOVEMENT_KEY = "dial_movement"
_PAYROLL_RATE_KEY = "per_character_rate"
_THRESHOLD_ID = re.compile(r"[a-z][a-z0-9_]*", re.ASCII)
_THRESHOLD_DIALS = {"budget", "morale"}


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
    if not directory.exists():
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
            for dial, delta in event["dials"].items():
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
