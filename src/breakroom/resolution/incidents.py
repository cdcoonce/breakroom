from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from breakroom.resolution.rng import RngStream, RollLog
from breakroom.worldstate import ValidationError

MAX_CASCADE_DEPTH = 5

REQUIRED_INCIDENT_FIELDS = ("id", "base_rate")
KNOWN_INCIDENT_FIELDS = {
    "id",
    "base_rate",
    "preconditions",
    "rooms",
    "characters",
    "effects",
    "chain_triggers",
}
KNOWN_PRECONDITION_FIELDS = {"path", "operator", "value"}
KNOWN_CHAIN_TRIGGER_FIELDS = {"target", "mode", "amount"}
VALID_OPERATORS = {"eq", "gte", "lte"}
VALID_CHAIN_MODES = {"boost", "direct"}


@dataclass(frozen=True)
class Precondition:
    path: str
    operator: str
    value: Any


@dataclass(frozen=True)
class ChainTrigger:
    target: str
    mode: str
    amount: float | None = None


@dataclass(frozen=True)
class IncidentDef:
    id: str
    base_rate: float
    preconditions: list[Precondition] = field(default_factory=list)
    rooms: list[str] = field(default_factory=list)
    characters: list[str] = field(default_factory=list)
    effects: list[dict[str, Any]] = field(default_factory=list)
    chain_triggers: list[ChainTrigger] = field(default_factory=list)


@dataclass(frozen=True)
class IncidentTable:
    incidents: dict[str, IncidentDef]


@dataclass(frozen=True)
class IncidentResolution:
    events: list[dict[str, Any]]
    cascades: list[dict[str, Any]]


def load_incident_table(world: Path) -> IncidentTable:
    relative = Path("data") / "incidents.toml"
    path = world / relative
    if not path.exists():
        raise ValidationError(f"{relative}: missing file")
    data = tomllib.loads(path.read_text())
    raw_incidents = data.get("incidents")
    if not isinstance(raw_incidents, list):
        raise ValidationError(f"{relative}: missing incidents")
    incidents: dict[str, IncidentDef] = {}
    for entry in raw_incidents:
        incident = _validate_incident(relative, entry)
        if incident.id in incidents:
            raise ValidationError(f"{relative}: duplicate incident id {incident.id}")
        incidents[incident.id] = incident
    for incident in incidents.values():
        for chain_trigger in incident.chain_triggers:
            if chain_trigger.target not in incidents:
                raise ValidationError(
                    f"{relative}: {incident.id} chain_triggers unknown target "
                    f"{chain_trigger.target!r}"
                )
    return IncidentTable(incidents=incidents)


def _validate_incident(relative: Path, entry: Any) -> IncidentDef:
    if not isinstance(entry, dict):
        raise ValidationError(f"{relative}: incident entry must be a table")
    unknown = set(entry) - KNOWN_INCIDENT_FIELDS
    if unknown:
        raise ValidationError(f"{relative}: unknown incident fields {sorted(unknown)}")
    for field_name in REQUIRED_INCIDENT_FIELDS:
        if field_name not in entry:
            raise ValidationError(f"{relative}: incident missing {field_name}")
    base_rate = entry["base_rate"]
    if not isinstance(base_rate, (int, float)) or not (0 <= base_rate <= 1):
        raise ValidationError(
            f"{relative}: base_rate must be between 0 and 1 for {entry['id']}"
        )
    return IncidentDef(
        id=entry["id"],
        base_rate=float(base_rate),
        preconditions=[
            _validate_precondition(relative, entry["id"], precondition)
            for precondition in entry.get("preconditions", [])
        ],
        rooms=list(entry.get("rooms", [])),
        characters=list(entry.get("characters", [])),
        effects=[dict(effect) for effect in entry.get("effects", [])],
        chain_triggers=[
            _validate_chain_trigger(relative, entry["id"], chain_trigger)
            for chain_trigger in entry.get("chain_triggers", [])
        ],
    )


def _validate_precondition(relative: Path, incident_id: str, entry: Any) -> Precondition:
    if not isinstance(entry, dict):
        raise ValidationError(f"{relative}: precondition entry must be a table for {incident_id}")
    unknown = set(entry) - KNOWN_PRECONDITION_FIELDS
    if unknown:
        raise ValidationError(
            f"{relative}: unknown precondition fields {sorted(unknown)} for {incident_id}"
        )
    for field_name in ("path", "operator", "value"):
        if field_name not in entry:
            raise ValidationError(
                f"{relative}: precondition missing {field_name} for {incident_id}"
            )
    if entry["operator"] not in VALID_OPERATORS:
        raise ValidationError(
            f"{relative}: invalid precondition operator {entry['operator']!r} for {incident_id}"
        )
    return Precondition(path=entry["path"], operator=entry["operator"], value=entry["value"])


def _validate_chain_trigger(relative: Path, incident_id: str, entry: Any) -> ChainTrigger:
    if not isinstance(entry, dict):
        raise ValidationError(f"{relative}: chain_trigger entry must be a table for {incident_id}")
    unknown = set(entry) - KNOWN_CHAIN_TRIGGER_FIELDS
    if unknown:
        raise ValidationError(
            f"{relative}: unknown chain_trigger fields {sorted(unknown)} for {incident_id}"
        )
    if "target" not in entry:
        raise ValidationError(f"{relative}: chain_trigger missing target for {incident_id}")
    mode = entry.get("mode", "direct")
    if mode not in VALID_CHAIN_MODES:
        raise ValidationError(
            f"{relative}: invalid chain_trigger mode {mode!r} for {incident_id}"
        )
    amount = entry.get("amount")
    if mode == "boost" and amount is None:
        raise ValidationError(
            f"{relative}: chain_trigger mode 'boost' requires numeric amount for {incident_id}"
        )
    if amount is not None and not isinstance(amount, (int, float)):
        raise ValidationError(
            f"{relative}: chain_trigger amount must be numeric for {incident_id}"
        )
    return ChainTrigger(
        target=entry["target"],
        mode=mode,
        amount=float(amount) if amount is not None else None,
    )


def evaluate_tick(
    table: IncidentTable,
    *,
    state: dict[str, Any],
    seed: int,
    tick: int,
    log: RollLog | None = None,
) -> IncidentResolution:
    rng = RngStream(seed=seed, stream="incidents", tick=tick, log=log)
    events: list[dict[str, Any]] = []
    cascades: list[dict[str, Any]] = []
    cascade_index = 0

    for incident_id in sorted(table.incidents):
        incident = table.incidents[incident_id]
        if not _eligible(incident, state):
            continue
        fired = rng.bernoulli(f"incident:{incident_id}:root", probability=incident.base_rate)
        if not fired:
            continue
        cascade_index += 1
        cascade_id = f"{tick}:{cascade_index}"
        members: list[dict[str, Any]] = []
        _chain(
            table=table,
            state=state,
            rng=rng,
            incident=incident,
            depth=0,
            trigger="root",
            cascade_id=cascade_id,
            tick=tick,
            members=members,
            events=events,
        )
        cascades.append(
            {
                "type": "cascade",
                "id": cascade_id,
                "tick": tick,
                "members": members,
                "depth": max(member["depth"] for member in members),
            }
        )

    return IncidentResolution(events=events, cascades=cascades)


def _chain(
    *,
    table: IncidentTable,
    state: dict[str, Any],
    rng: RngStream,
    incident: IncidentDef,
    depth: int,
    trigger: str,
    cascade_id: str,
    tick: int,
    members: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> None:
    members.append({"incident_id": incident.id, "depth": depth, "trigger": trigger})
    events.extend(
        {
            **effect,
            "incident_id": incident.id,
            "cascade_id": cascade_id,
            "depth": depth,
            "tick": tick,
        }
        for effect in incident.effects
    )

    for chain_trigger in incident.chain_triggers:
        child_depth = depth + 1
        if child_depth > MAX_CASCADE_DEPTH:
            continue
        target = table.incidents[chain_trigger.target]
        if not _eligible(target, state):
            continue
        if chain_trigger.mode == "direct":
            fired = True
        else:
            probability = min(1.0, max(0.0, target.base_rate + (chain_trigger.amount or 0.0)))
            fired = rng.bernoulli(
                f"incident:{target.id}:cascade:{child_depth}",
                probability=probability,
            )
        if not fired:
            continue
        _chain(
            table=table,
            state=state,
            rng=rng,
            incident=target,
            depth=child_depth,
            trigger=chain_trigger.mode,
            cascade_id=cascade_id,
            tick=tick,
            members=members,
            events=events,
        )


def _eligible(incident: IncidentDef, state: dict[str, Any]) -> bool:
    return all(_check_precondition(state, precondition) for precondition in incident.preconditions)


def _check_precondition(state: dict[str, Any], precondition: Precondition) -> bool:
    value, found = _resolve_path(state, precondition.path)
    if not found:
        return False
    if precondition.operator == "eq":
        return value == precondition.value
    if precondition.operator == "gte":
        return value >= precondition.value
    return value <= precondition.value


def _resolve_path(state: dict[str, Any], path: str) -> tuple[Any, bool]:
    current: Any = state
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None, False
        current = current[part]
    return current, True
