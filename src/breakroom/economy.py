"""Versioned dial movement and its world-specific rulebook."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Any

from breakroom.worldstate import ValidationError

_DIALS = {"budget", "morale", "reputation"}
_MOVEMENT_KEY = "dial_movement"


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
        try:
            text = files("breakroom").joinpath("data/economy.toml").read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValidationError(f"bundled economy rulebook: cannot read: {exc}") from exc
        context = "bundled economy rulebook"
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
            event, rulebook if rulebook is not None else load_rulebook(Path("."))
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
    if (
        not isinstance(incident, dict)
        or set(incident) != {"dial", "amount_path"}
        or incident.get("dial") not in _DIALS
        or incident.get("amount_path") != "incident.morale_delta"
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
