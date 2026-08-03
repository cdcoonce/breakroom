from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from breakroom.worldstate import ValidationError

REQUIRED_NORM_FIELDS = (
    "id",
    "scope",
    "description",
    "severity",
    "detection",
    "tags",
    "related_values",
)
VALID_SCOPES = {"tower_policy", "social_norm", "character_duty"}
VALID_SEVERITIES = {"minor", "moderate", "major", "severe"}
STATUS_RELEVANT_AUDIENCE = {"manager", "client", "all-hands"}


@dataclass(frozen=True)
class Norm:
    id: str
    scope: str
    description: str
    severity: str
    detection: str
    tags: list[str]
    related_values: list[str]
    applies_to_roles: list[str] = field(default_factory=list)
    applies_to_rooms: list[str] = field(default_factory=list)
    cooldown_days: int | None = None


@dataclass(frozen=True)
class Registry:
    norms: dict[str, Norm]


def load_registry(world: Path) -> Registry:
    relative = Path("data") / "norms.toml"
    path = world / relative
    if not path.exists():
        raise ValidationError(f"{relative}: missing file")
    data = tomllib.loads(path.read_text())
    raw_norms = data.get("norms")
    if not isinstance(raw_norms, list):
        raise ValidationError(f"{relative}: missing norms")
    norms: dict[str, Norm] = {}
    for entry in raw_norms:
        norm = _validate_norm(relative, entry)
        if norm.id in norms:
            raise ValidationError(f"{relative}: duplicate norm id {norm.id}")
        norms[norm.id] = norm
    return Registry(norms=norms)


def _validate_norm(relative: Path, entry: Any) -> Norm:
    if not isinstance(entry, dict):
        raise ValidationError(f"{relative}: norm entry must be a table")
    for field_name in REQUIRED_NORM_FIELDS:
        if field_name not in entry:
            raise ValidationError(f"{relative}: missing {field_name}")
    if entry["scope"] not in VALID_SCOPES:
        raise ValidationError(f"{relative}: invalid scope {entry['scope']!r} for {entry['id']}")
    if entry["severity"] not in VALID_SEVERITIES:
        raise ValidationError(
            f"{relative}: invalid severity {entry['severity']!r} for {entry['id']}"
        )
    if not isinstance(entry["tags"], list):
        raise ValidationError(f"{relative}: tags must be a list for {entry['id']}")
    if not isinstance(entry["related_values"], list):
        raise ValidationError(f"{relative}: related_values must be a list for {entry['id']}")
    if entry["detection"] not in _DETECTORS:
        raise ValidationError(
            f"{relative}: invalid detection {entry['detection']!r} for {entry['id']}"
        )
    return Norm(
        id=entry["id"],
        scope=entry["scope"],
        description=entry["description"],
        severity=entry["severity"],
        detection=entry["detection"],
        tags=list(entry["tags"]),
        related_values=list(entry["related_values"]),
        applies_to_roles=list(entry.get("applies_to_roles", [])),
        applies_to_rooms=list(entry.get("applies_to_rooms", [])),
        cooldown_days=entry.get("cooldown_days"),
    )


def tag_record(
    registry: Registry,
    record: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    norm_tags: list[str] = []
    norm_violations: list[dict[str, Any]] = []
    for norm in registry.norms.values():
        detector = _DETECTORS.get(norm.detection)
        if detector is None:
            continue
        evidence = detector(record, state=state, events=events)
        if evidence is None:
            continue
        for tag in norm.tags:
            if tag not in norm_tags:
                norm_tags.append(tag)
        norm_violations.append(
            {
                "norm_id": norm.id,
                "severity": norm.severity,
                "detected_by": norm.detection,
                "evidence": evidence,
            }
        )
    return {"norm_tags": norm_tags, "norm_violations": norm_violations}


def integrity_drift(
    character: dict[str, Any],
    registry: Registry,
    norm_violations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    declared = set(character.get("declared_values", []))
    drift: list[dict[str, Any]] = []
    for violation in norm_violations:
        norm = registry.norms.get(violation["norm_id"])
        if norm is None:
            continue
        matched = sorted(declared & set(norm.related_values))
        if not matched:
            continue
        drift.append(
            {
                "character_id": character.get("id"),
                "norm_id": norm.id,
                "declared_values": matched,
            }
        )
    return drift


def _expense_claim_overstated(
    record: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if record.get("type") != "decision" or record.get("action") != "submit_expense_claim":
        return None
    claim = record.get("expense_claim")
    if not isinstance(claim, dict):
        return None
    incurred = claim.get("amount_incurred")
    reported = claim.get("amount_reported")
    if not isinstance(incurred, (int, float)) or not isinstance(reported, (int, float)):
        return None
    if reported <= incurred:
        return None
    return {
        "character_id": record.get("character_id"),
        "amount_incurred": incurred,
        "amount_reported": reported,
        "receipt_id": claim.get("receipt_id"),
    }


def _public_claim_omits_contributors(
    record: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if record.get("type") != "decision" or record.get("action") != "present_work":
        return None
    actual = record.get("actual_contributors")
    claimed = record.get("claimed_contributors")
    audience = record.get("audience")
    if not isinstance(actual, list) or not isinstance(claimed, list):
        return None
    if not isinstance(audience, list) or not audience:
        return None
    actual_set = set(actual)
    claimed_set = set(claimed)
    if not claimed_set < actual_set:
        return None
    if not set(audience) & STATUS_RELEVANT_AUDIENCE:
        return None
    return {
        "character_id": record.get("character_id"),
        "work_item_id": record.get("work_item_id"),
        "omitted_contributors": sorted(actual_set - claimed_set),
        "audience": audience,
    }


def _assigned_shift_unattended(
    record: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if record.get("type") != "location_observation":
        return None
    duty_id = record.get("during_duty_id")
    character_id = record.get("character_id")
    day = record.get("day")
    if duty_id is None or character_id is None:
        return None
    assignments = (state or {}).get("assignments", [])
    assignment = next(
        (
            assignment
            for assignment in assignments
            if assignment.get("character_id") == character_id
            and assignment.get("duty_id") == duty_id
            and assignment.get("day") == day
        ),
        None,
    )
    if assignment is None:
        return None
    attended = any(
        prior.get("type") == "duty_attended"
        and prior.get("character_id") == character_id
        and prior.get("duty_id") == duty_id
        and prior.get("day") == day
        for prior in (events if events is not None else [])
    )
    if attended:
        return None
    if record.get("room_id") == assignment.get("required_room"):
        return None
    return {
        "character_id": character_id,
        "duty_id": duty_id,
        "day": day,
        "required_room": assignment.get("required_room"),
        "observed_room": record.get("room_id"),
    }


def _secret_shared_with_unauthorized_audience(
    record: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if record.get("type") != "decision" or record.get("action") != "share_secret":
        return None
    if record.get("secret_classification") != "client_confidential":
        return None
    authorized = record.get("authorized_audience")
    actual = record.get("actual_audience")
    if not isinstance(authorized, list) or not isinstance(actual, list):
        return None
    unauthorized = sorted(set(actual) - set(authorized))
    if not unauthorized:
        return None
    return {
        "character_id": record.get("character_id"),
        "secret_id": record.get("secret_id"),
        "secret_classification": record.get("secret_classification"),
        "unauthorized_audience": unauthorized,
    }


def _incident_cleanup_owner_missed(
    record: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if record.get("type") != "incident":
        return None
    incident = record.get("incident")
    if not isinstance(incident, dict):
        return None
    owner = incident.get("cleanup_owner")
    if not owner:
        return None
    if incident.get("resolved", False):
        return None
    return {
        "incident_id": incident.get("id"),
        "room_id": incident.get("room"),
        "cleanup_owner": owner,
        "resolved": incident.get("resolved", False),
    }


_DETECTORS: dict[str, Callable[..., dict[str, Any] | None]] = {
    "expense_claim_overstated": _expense_claim_overstated,
    "public_claim_omits_contributors": _public_claim_omits_contributors,
    "assigned_shift_unattended": _assigned_shift_unattended,
    "secret_shared_with_unauthorized_audience": _secret_shared_with_unauthorized_audience,
    "incident_cleanup_owner_missed": _incident_cleanup_owner_missed,
}
