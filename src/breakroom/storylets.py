"""Storylet registry, eligibility, salience, and seeded draw.

Pure engine per `docs/design/storylets.md`. Nothing here reads the chronicle, emits
events, or runs decision calls: it loads `data/storylets/*.toml`, filters the registry
against structured state, scores what survives, and draws one spotlight storylet with
its cast. Wiring the result into the tick loop is a later slice.

Two semantics are worth naming because the design doc leaves them implicit:

Candidate pool. Quality preconditions evaluate against the union of every declared
slot's *raw* candidate list — the slot's `source` resolved against current state with
`required`/`max_count` ignored and no RNG draw. A storylet is eligible on a quality
precondition when at least one character in that pool satisfies it. Slot-filling then
prefers, within each slot's own candidate list, characters that satisfy the storylet's
quality preconditions over ones that do not.

Candidate rooms. A storylet that declares `incident_ids` plays in the rooms of the
current-tick incidents it matches; one that does not can play in any room of the tower.
`room_kinds`, `same_room`, and `same_floor` all read that set.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from breakroom.resolution.rng import RngStream, RollLog
from breakroom.secrets import NEAR_EXPOSURE_THRESHOLD
from breakroom.worldstate import ValidationError, character_edges, ticks_since_spotlight

SALIENCE_FLOOR = 0.1
LOW_MORALE_THRESHOLD = 25
DEFAULT_BUDGET_WARNING_LINE = 250
EDGE_HEAT_WINDOW_TICKS = 7
STALE_INCIDENT_WINDOW_TICKS = 2
DEADLINE_PRESSURE_WINDOW_TICKS = 2

SELECT_STREAM = "storylet_select"
PARTICIPANT_STREAM = "storylet_participant"

REQUIRED_STORYLET_FIELDS = ("id", "title", "premise", "kind", "eligibility", "participants")
KNOWN_STORYLET_FIELDS = {
    "id",
    "title",
    "premise",
    "kind",
    "eligibility",
    "participants",
    "decision_points",
    "effect_hooks",
    "salience",
}
VALID_STORYLET_KINDS = {
    "incident_response",
    "norm_pressure",
    "duty_conflict",
    "social_disclosure",
    "resource_favor",
    "ambient",
}
KNOWN_PRECONDITION_FIELDS = {
    "incident_ids",
    "room_kinds",
    "required_quality_any",
    "forbidden_state_any",
    "min_tick_gap",
}
KNOWN_QUALITY_TEST_FIELDS = {"quality", "operator", "value"}
# `present` is the bare-string form: `"state:offstage"` tests presence, not equality with
# `true`, so a scalar quality still reads as present at any value in `-3..+3`.
VALID_OPERATORS = {"present", "eq", "gte", "lte"}
VALID_QUALITY_NAMESPACES = {
    "stat",
    "trait",
    "state",
    "skill",
    "value",
    "role",
    "rel",
    "room",
}
KNOWN_PARTICIPANT_FIELDS = {"slot", "source", "required", "max_count"}
VALID_PARTICIPANT_SOURCES = {
    "incident.cleanup_owner",
    "incident.affected_character",
    "same_room",
    "same_floor",
    "relationship_edge",
    "assignment_team",
    "secret_knower",
}
KNOWN_DECISION_POINT_FIELDS = {"id", "decision_type", "character_slot"}
VALID_DECISION_TYPES = {
    "incident_response",
    "norm_pressure",
    "duty_conflict",
    "social_disclosure",
    "resource_favor",
}
KNOWN_EFFECT_HOOK_FIELDS = {"hook", "when"}
VALID_HOOK_PHASES = {"after_decision", "after_resolution", "after_scene"}
KNOWN_SALIENCE_FIELDS = {"storylet_bias"}
STORYLET_BIAS_BOUNDS = (-2.0, 2.0)


@dataclass(frozen=True)
class QualityTest:
    quality: str
    operator: str = "present"
    value: Any = None


@dataclass(frozen=True)
class Eligibility:
    incident_ids: tuple[str, ...] = ()
    room_kinds: tuple[str, ...] = ()
    required_quality_any: tuple[QualityTest, ...] = ()
    forbidden_state_any: tuple[QualityTest, ...] = ()
    min_tick_gap: int | None = None


@dataclass(frozen=True)
class ParticipantSlot:
    slot: str
    source: str
    required: bool = False
    max_count: int = 1


@dataclass(frozen=True)
class DecisionPoint:
    id: str
    decision_type: str
    character_slot: str


@dataclass(frozen=True)
class EffectHook:
    hook: str
    when: str


@dataclass(frozen=True)
class StoryletDef:
    id: str
    title: str
    premise: str
    kind: str
    eligibility: Eligibility
    participants: tuple[ParticipantSlot, ...]
    decision_points: tuple[DecisionPoint, ...] = ()
    effect_hooks: tuple[EffectHook, ...] = ()
    storylet_bias: float = 0.0


@dataclass(frozen=True)
class StoryletRegistry:
    storylets: dict[str, StoryletDef]


@dataclass(frozen=True)
class EngineContext:
    """Structured state the engine may inspect. Every field defaults to empty.

    `incident_events` holds emitted incident *events* (`{"type", "day", "incident"}`);
    participant sources and the fresh-incident weight read the `incident` payload, which
    is where `tick.py` sets `cleanup_owner`. `edge_delta_events` holds emitted
    `edge_delta` events, whose `day` is what bounds the edge-heat window.
    """

    tick: int
    state: dict[str, Any] = field(default_factory=dict)
    characters: dict[str, dict[str, Any]] = field(default_factory=dict)
    character_rooms: dict[str, str] = field(default_factory=dict)
    incident_events: list[dict[str, Any]] = field(default_factory=list)
    secrets: list[dict[str, Any]] = field(default_factory=list)
    edge_delta_events: list[dict[str, Any]] = field(default_factory=list)
    director_actions: list[dict[str, Any]] = field(default_factory=list)
    storylet_history: dict[str, int] = field(default_factory=dict)
    assignments: dict[str, list[str]] = field(default_factory=dict)
    contract_deadlines: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class StoryletSelection:
    storylet: StoryletDef
    score: float
    participants: dict[str, list[str]]
    decision_bindings: tuple[dict[str, Any], ...]


# --- registry -------------------------------------------------------------------------


def load_registry(world: Path) -> StoryletRegistry:
    relative = Path("data") / "storylets"
    directory = world / relative
    if not directory.is_dir():
        raise ValidationError(f"{relative}: missing directory")
    storylets: dict[str, StoryletDef] = {}
    for path in sorted(directory.glob("*.toml")):
        source = str(relative / path.name)
        storylet = _validate_storylet(source, tomllib.loads(path.read_text()))
        if storylet.id in storylets:
            raise ValidationError(f"{source}: duplicate storylet id {storylet.id}")
        storylets[storylet.id] = storylet
    return StoryletRegistry(storylets=storylets)


def _validate_storylet(source: str, entry: Any) -> StoryletDef:
    if not isinstance(entry, dict):
        raise ValidationError(f"{source}: storylet must be a table")
    unknown = set(entry) - KNOWN_STORYLET_FIELDS
    if unknown:
        raise ValidationError(f"{source}: unknown storylet fields {sorted(unknown)}")
    for field_name in REQUIRED_STORYLET_FIELDS:
        if field_name not in entry:
            raise ValidationError(f"{source}: storylet missing {field_name}")
    storylet_id = entry["id"]
    if not isinstance(storylet_id, str) or not storylet_id:
        raise ValidationError(f"{source}: storylet id must be a non-empty string")
    for field_name in ("title", "premise"):
        if not isinstance(entry[field_name], str) or not entry[field_name]:
            raise ValidationError(f"{source}: {field_name} must be a non-empty string")
    kind = entry["kind"]
    if kind not in VALID_STORYLET_KINDS:
        raise ValidationError(f"{source}: invalid storylet kind {kind!r} for {storylet_id}")

    participants = _validate_participants(source, storylet_id, entry["participants"])
    decision_points = _validate_decision_points(
        source, storylet_id, kind, entry.get("decision_points", []), participants
    )
    return StoryletDef(
        id=storylet_id,
        title=entry["title"],
        premise=entry["premise"],
        kind=kind,
        eligibility=_validate_eligibility(source, storylet_id, entry["eligibility"]),
        participants=participants,
        decision_points=decision_points,
        effect_hooks=_validate_effect_hooks(source, storylet_id, entry.get("effect_hooks", [])),
        storylet_bias=_validate_salience(source, storylet_id, entry.get("salience", {})),
    )


def _validate_eligibility(source: str, storylet_id: str, entry: Any) -> Eligibility:
    if not isinstance(entry, dict):
        raise ValidationError(f"{source}: eligibility must be a table for {storylet_id}")
    unknown = set(entry) - KNOWN_PRECONDITION_FIELDS
    if unknown:
        raise ValidationError(
            f"{source}: unknown precondition fields {sorted(unknown)} for {storylet_id}"
        )
    min_tick_gap = entry.get("min_tick_gap")
    if min_tick_gap is not None:
        if isinstance(min_tick_gap, bool) or not isinstance(min_tick_gap, int) or min_tick_gap < 0:
            raise ValidationError(
                f"{source}: min_tick_gap must be a non-negative integer for {storylet_id}"
            )
    return Eligibility(
        incident_ids=_string_tuple(source, storylet_id, "incident_ids", entry.get("incident_ids")),
        room_kinds=_string_tuple(source, storylet_id, "room_kinds", entry.get("room_kinds")),
        required_quality_any=_quality_tests(
            source, storylet_id, "required_quality_any", entry.get("required_quality_any")
        ),
        forbidden_state_any=_quality_tests(
            source, storylet_id, "forbidden_state_any", entry.get("forbidden_state_any")
        ),
        min_tick_gap=min_tick_gap,
    )


def _string_tuple(source: str, storylet_id: str, field_name: str, value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(
            f"{source}: {field_name} must be a list of strings for {storylet_id}"
        )
    return tuple(value)


def _quality_tests(
    source: str, storylet_id: str, field_name: str, value: Any
) -> tuple[QualityTest, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValidationError(f"{source}: {field_name} must be a list for {storylet_id}")
    return tuple(
        _quality_test(source, storylet_id, field_name, entry) for entry in value
    )


def _quality_test(source: str, storylet_id: str, field_name: str, entry: Any) -> QualityTest:
    if isinstance(entry, str):
        _check_quality_namespace(source, storylet_id, entry)
        return QualityTest(quality=entry)
    if not isinstance(entry, dict):
        raise ValidationError(
            f"{source}: {field_name} entries must be strings or tables for {storylet_id}"
        )
    unknown = set(entry) - KNOWN_QUALITY_TEST_FIELDS
    if unknown:
        raise ValidationError(
            f"{source}: unknown {field_name} fields {sorted(unknown)} for {storylet_id}"
        )
    quality = entry.get("quality")
    if not isinstance(quality, str):
        raise ValidationError(f"{source}: {field_name} missing quality for {storylet_id}")
    _check_quality_namespace(source, storylet_id, quality)
    operator = entry.get("operator", "present")
    if operator not in VALID_OPERATORS:
        raise ValidationError(
            f"{source}: invalid quality operator {operator!r} for {storylet_id}"
        )
    if operator != "present" and "value" not in entry:
        raise ValidationError(
            f"{source}: {field_name} operator {operator!r} requires value for {storylet_id}"
        )
    return QualityTest(quality=quality, operator=operator, value=entry.get("value"))


def _check_quality_namespace(source: str, storylet_id: str, quality: str) -> None:
    namespace, sep, name = quality.partition(":")
    if not sep or not name or namespace not in VALID_QUALITY_NAMESPACES:
        raise ValidationError(
            f"{source}: invalid quality namespace {quality!r} for {storylet_id}"
        )


def _validate_participants(
    source: str, storylet_id: str, value: Any
) -> tuple[ParticipantSlot, ...]:
    if not isinstance(value, list) or not value:
        raise ValidationError(
            f"{source}: participants must be a non-empty list for {storylet_id}"
        )
    slots: list[ParticipantSlot] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise ValidationError(f"{source}: participant entry must be a table for {storylet_id}")
        unknown = set(entry) - KNOWN_PARTICIPANT_FIELDS
        if unknown:
            raise ValidationError(
                f"{source}: unknown participant fields {sorted(unknown)} for {storylet_id}"
            )
        for field_name in ("slot", "source"):
            if not isinstance(entry.get(field_name), str):
                raise ValidationError(
                    f"{source}: participant missing {field_name} for {storylet_id}"
                )
        if entry["source"] not in VALID_PARTICIPANT_SOURCES:
            raise ValidationError(
                f"{source}: invalid participant source {entry['source']!r} for {storylet_id}"
            )
        if entry["slot"] in seen:
            raise ValidationError(
                f"{source}: duplicate participant slot {entry['slot']!r} for {storylet_id}"
            )
        seen.add(entry["slot"])
        required = entry.get("required", False)
        if not isinstance(required, bool):
            raise ValidationError(
                f"{source}: participant required must be a bool for {storylet_id}"
            )
        max_count = entry.get("max_count", 1)
        if isinstance(max_count, bool) or not isinstance(max_count, int) or max_count < 1:
            raise ValidationError(
                f"{source}: participant max_count must be a positive integer for {storylet_id}"
            )
        slots.append(
            ParticipantSlot(
                slot=entry["slot"],
                source=entry["source"],
                required=required,
                max_count=max_count,
            )
        )
    return tuple(slots)


def _validate_decision_points(
    source: str,
    storylet_id: str,
    kind: str,
    value: Any,
    participants: tuple[ParticipantSlot, ...],
) -> tuple[DecisionPoint, ...]:
    if not isinstance(value, list):
        raise ValidationError(f"{source}: decision_points must be a list for {storylet_id}")
    if kind == "ambient" and value:
        raise ValidationError(
            f"{source}: ambient storylet {storylet_id} must not declare decision_points"
        )
    if kind != "ambient" and not value:
        raise ValidationError(f"{source}: storylet {storylet_id} missing decision_points")
    slot_names = {slot.slot for slot in participants}
    points: list[DecisionPoint] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValidationError(
                f"{source}: decision_point entry must be a table for {storylet_id}"
            )
        unknown = set(entry) - KNOWN_DECISION_POINT_FIELDS
        if unknown:
            raise ValidationError(
                f"{source}: unknown decision_point fields {sorted(unknown)} for {storylet_id}"
            )
        for field_name in KNOWN_DECISION_POINT_FIELDS:
            if not isinstance(entry.get(field_name), str):
                raise ValidationError(
                    f"{source}: decision_point missing {field_name} for {storylet_id}"
                )
        if entry["decision_type"] not in VALID_DECISION_TYPES:
            raise ValidationError(
                f"{source}: invalid decision_type {entry['decision_type']!r} for {storylet_id}"
            )
        if entry["character_slot"] not in slot_names:
            raise ValidationError(
                f"{source}: decision_point character_slot {entry['character_slot']!r} "
                f"is not a declared slot for {storylet_id}"
            )
        points.append(
            DecisionPoint(
                id=entry["id"],
                decision_type=entry["decision_type"],
                character_slot=entry["character_slot"],
            )
        )
    return tuple(points)


def _validate_effect_hooks(source: str, storylet_id: str, value: Any) -> tuple[EffectHook, ...]:
    if not isinstance(value, list):
        raise ValidationError(f"{source}: effect_hooks must be a list for {storylet_id}")
    hooks: list[EffectHook] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValidationError(f"{source}: effect_hook entry must be a table for {storylet_id}")
        unknown = set(entry) - KNOWN_EFFECT_HOOK_FIELDS
        if unknown:
            raise ValidationError(
                f"{source}: unknown effect_hook fields {sorted(unknown)} for {storylet_id}"
            )
        if not isinstance(entry.get("hook"), str):
            raise ValidationError(f"{source}: effect_hook missing hook for {storylet_id}")
        when = entry.get("when")
        if when not in VALID_HOOK_PHASES:
            raise ValidationError(
                f"{source}: invalid effect_hook when {when!r} for {storylet_id}"
            )
        hooks.append(EffectHook(hook=entry["hook"], when=when))
    return tuple(hooks)


def _validate_salience(source: str, storylet_id: str, value: Any) -> float:
    if not isinstance(value, dict):
        raise ValidationError(f"{source}: salience must be a table for {storylet_id}")
    unknown = set(value) - KNOWN_SALIENCE_FIELDS
    if unknown:
        raise ValidationError(
            f"{source}: unknown salience fields {sorted(unknown)} for {storylet_id}"
        )
    bias = value.get("storylet_bias", 0.0)
    low, high = STORYLET_BIAS_BOUNDS
    if isinstance(bias, bool) or not isinstance(bias, (int, float)) or not low <= bias <= high:
        raise ValidationError(
            f"{source}: storylet_bias must be between {low} and {high} for {storylet_id}"
        )
    return float(bias)


# --- eligibility ----------------------------------------------------------------------


def eligible_storylets(
    registry: StoryletRegistry, *, context: EngineContext
) -> list[StoryletDef]:
    """Registry order in, registry order out. The draw imposes the id sort."""
    return [
        storylet
        for storylet in registry.storylets.values()
        if _is_eligible(storylet, context)
    ]


def _is_eligible(storylet: StoryletDef, context: EngineContext) -> bool:
    eligibility = storylet.eligibility
    if eligibility.incident_ids and not _matched_incidents(storylet, context):
        return False
    if eligibility.room_kinds:
        kinds = {room.get("kind") for room in _candidate_rooms(storylet, context)}
        if not set(eligibility.room_kinds) & kinds:
            return False
    if eligibility.min_tick_gap is not None:
        last_tick = context.storylet_history.get(storylet.id)
        if last_tick is not None and context.tick - last_tick < eligibility.min_tick_gap:
            return False

    slot_candidates = _slot_candidate_lists(storylet, context)
    for slot in storylet.participants:
        if slot.required and not slot_candidates[slot.slot]:
            return False

    pool = _candidate_pool(slot_candidates)
    if eligibility.required_quality_any and not any(
        _matches_any(context, character_id, eligibility.required_quality_any)
        for character_id in pool
    ):
        return False
    if eligibility.forbidden_state_any and not any(
        not _matches_any(context, character_id, eligibility.forbidden_state_any)
        for character_id in pool
    ):
        return False
    return True


def _matches_any(context: EngineContext, character_id: str, tests: tuple[QualityTest, ...]) -> bool:
    qualities = context.characters.get(character_id, {}).get("qualities", {})
    if not isinstance(qualities, dict):
        return False
    return any(_quality_matches(qualities, test) for test in tests)


def _quality_matches(qualities: dict[str, Any], test: QualityTest) -> bool:
    if test.quality not in qualities:
        return False
    value = qualities[test.quality]
    if test.operator == "present":
        return True
    if test.operator == "eq":
        return bool(value == test.value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not isinstance(test.value, (int, float)) or isinstance(test.value, bool):
        return False
    if test.operator == "gte":
        return value >= test.value
    return value <= test.value


def _satisfies_quality_preconditions(
    storylet: StoryletDef, context: EngineContext, character_id: str
) -> bool:
    """Joint castability, used only to rank a slot's own candidates before the draw."""
    eligibility = storylet.eligibility
    if eligibility.required_quality_any and not _matches_any(
        context, character_id, eligibility.required_quality_any
    ):
        return False
    if eligibility.forbidden_state_any and _matches_any(
        context, character_id, eligibility.forbidden_state_any
    ):
        return False
    return True


def _incident_payload(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        return {}
    payload = event.get("incident")
    return payload if isinstance(payload, dict) else {}


def _current_incidents(context: EngineContext) -> list[dict[str, Any]]:
    return [
        _incident_payload(event)
        for event in context.incident_events
        if isinstance(event, dict) and event.get("day") == context.tick
    ]


def _matched_incidents(storylet: StoryletDef, context: EngineContext) -> list[dict[str, Any]]:
    """Incident payloads this storylet consumes: this tick's, plus stale unresolved ones.

    A storylet naming `incident_ids` stays castable while the incident it consumes is
    still open. That is what `fresh_incident_weight`'s `+2.0` prior-two-ticks branch
    assumes; scoping the match to the current tick alone would make it unreachable.
    """
    wanted = set(storylet.eligibility.incident_ids)
    if not wanted:
        return _current_incidents(context)
    matched: list[dict[str, Any]] = []
    for event in context.incident_events:
        if not isinstance(event, dict):
            continue
        day = event.get("day")
        if not isinstance(day, int):
            continue
        payload = _incident_payload(event)
        if payload.get("id") not in wanted:
            continue
        age = context.tick - day
        if age == 0:
            matched.append(payload)
        elif 1 <= age <= STALE_INCIDENT_WINDOW_TICKS and not payload.get("resolved"):
            matched.append(payload)
    return matched


def _rooms(context: EngineContext) -> list[dict[str, Any]]:
    rooms = context.state.get("rooms", [])
    if not isinstance(rooms, list):
        return []
    return [room for room in rooms if isinstance(room, dict)]


def _candidate_rooms(storylet: StoryletDef, context: EngineContext) -> list[dict[str, Any]]:
    rooms = _rooms(context)
    if not storylet.eligibility.incident_ids:
        return rooms
    room_ids = {payload.get("room") for payload in _matched_incidents(storylet, context)}
    return [room for room in rooms if room.get("id") in room_ids]


def _character_ids(context: EngineContext) -> list[str]:
    declared = context.state.get("characters")
    if isinstance(declared, list):
        ordered = [item for item in declared if isinstance(item, str)]
        if ordered:
            return ordered
    return sorted(context.characters)


def _reveal_safe_secrets(context: EngineContext) -> list[dict[str, Any]]:
    """Observable secrets plus the ones near exposure, as reveal-safe metadata."""
    safe = []
    for secret in context.secrets:
        if not isinstance(secret, dict):
            continue
        risk = secret.get("exposure_risk", 0.0)
        near = isinstance(risk, (int, float)) and risk >= NEAR_EXPOSURE_THRESHOLD
        if secret.get("state") == "observable" or near:
            safe.append(secret)
    return safe


def _slot_candidate_lists(
    storylet: StoryletDef, context: EngineContext
) -> dict[str, list[str]]:
    """Raw candidates per slot: source resolved, `required`/`max_count` ignored, no RNG."""
    lists: dict[str, list[str]] = {}
    anchors: list[str] = []
    for slot in storylet.participants:
        candidates = _resolve_source(slot.source, storylet, context, anchors)
        lists[slot.slot] = candidates
        anchors.extend(candidate for candidate in candidates if candidate not in anchors)
    return lists


def _resolve_source(
    source: str, storylet: StoryletDef, context: EngineContext, anchors: list[str]
) -> list[str]:
    if source == "incident.cleanup_owner":
        return _incident_field(storylet, context, "cleanup_owner")
    if source == "incident.affected_character":
        return _incident_field(storylet, context, "affected_character")
    if source == "same_room":
        room_ids = {room.get("id") for room in _candidate_rooms(storylet, context)}
        return [
            character_id
            for character_id in _character_ids(context)
            if context.character_rooms.get(character_id) in room_ids
        ]
    if source == "same_floor":
        floors = {room.get("floor") for room in _candidate_rooms(storylet, context)}
        room_floors = {room.get("id"): room.get("floor") for room in _rooms(context)}
        return [
            character_id
            for character_id in _character_ids(context)
            if room_floors.get(context.character_rooms.get(character_id)) in floors
        ]
    if source == "relationship_edge":
        return _relationship_edge_candidates(context, anchors)
    if source == "assignment_team":
        return _assignment_team_candidates(context, anchors)
    if source == "secret_knower":
        knowers: set[str] = set()
        for secret in _reveal_safe_secrets(context):
            entries = secret.get("knowers", [])
            if isinstance(entries, list):
                knowers.update(entry for entry in entries if isinstance(entry, str))
        return [
            character_id for character_id in _character_ids(context) if character_id in knowers
        ]
    return []


def _incident_field(
    storylet: StoryletDef, context: EngineContext, field_name: str
) -> list[str]:
    candidates: list[str] = []
    for payload in _matched_incidents(storylet, context):
        value = payload.get(field_name)
        if isinstance(value, str) and value not in candidates:
            candidates.append(value)
    return candidates


def _relationship_edge_candidates(context: EngineContext, anchors: list[str]) -> list[str]:
    """Characters connected by an edge to an earlier slot's cast, or to anyone at all."""
    anchor_set = set(anchors)
    candidates: list[str] = []
    for character_id in _character_ids(context):
        edges = character_edges(context.state, character_id)
        if not edges:
            continue
        if not anchor_set:
            candidates.append(character_id)
            continue
        partners = {
            edge["from"] if edge["to"] == character_id else edge["to"] for edge in edges
        }
        if partners & anchor_set:
            candidates.append(character_id)
    return candidates


def _assignment_team_candidates(context: EngineContext, anchors: list[str]) -> list[str]:
    anchor_set = set(anchors)
    teammates: set[str] = set()
    for members in context.assignments.values():
        if not isinstance(members, list):
            continue
        team = {member for member in members if isinstance(member, str)}
        if not anchor_set or team & anchor_set:
            teammates.update(team)
    return [character_id for character_id in _character_ids(context) if character_id in teammates]


def _candidate_pool(slot_candidates: dict[str, list[str]]) -> list[str]:
    pool: list[str] = []
    for candidates in slot_candidates.values():
        for candidate in candidates:
            if candidate not in pool:
                pool.append(candidate)
    return pool


# --- salience -------------------------------------------------------------------------


def salience_score(storylet: StoryletDef, *, context: EngineContext) -> float:
    """`docs/design/storylets.md` §Salience Score, floored so everything stays drawable."""
    pool = _candidate_pool(_slot_candidate_lists(storylet, context))
    raw = (
        1.0
        + _fresh_incident_weight(storylet, context)
        + _hot_edge_weight(context, pool)
        + _exposure_weight(context, pool)
        + _pressure_weight(context)
        + _intervention_weight(storylet, context, pool)
        + _time_since_spotlight_weight(context, pool)
        + storylet.storylet_bias
    )
    return max(SALIENCE_FLOOR, raw)


def _fresh_incident_weight(storylet: StoryletDef, context: EngineContext) -> float:
    wanted = set(storylet.eligibility.incident_ids)
    if not wanted:
        return 0.0
    if any(payload.get("id") in wanted for payload in _current_incidents(context)):
        return 4.0
    if _matched_incidents(storylet, context):
        return 2.0
    return 0.0


def _hot_edge_weight(context: EngineContext, pool: list[str]) -> float:
    pool_set = set(pool)
    total = 0.0
    for event in context.edge_delta_events:
        if not isinstance(event, dict):
            continue
        day = event.get("day")
        if not isinstance(day, int) or not 0 <= context.tick - day < EDGE_HEAT_WINDOW_TICKS:
            continue
        if event.get("from") not in pool_set and event.get("to") not in pool_set:
            continue
        edges = event.get("edges", {})
        if not isinstance(edges, dict):
            continue
        for change in edges.values():
            if not isinstance(change, dict):
                continue
            delta = change.get("delta", 0)
            if isinstance(delta, (int, float)) and not isinstance(delta, bool):
                total += abs(delta)
    return min(3.0, 0.5 * total)


def _exposure_weight(context: EngineContext, pool: list[str]) -> float:
    pool_set = set(pool)
    risks = [0.0]
    for secret in context.secrets:
        if not isinstance(secret, dict):
            continue
        knowers = secret.get("knowers", [])
        knower_set = set(knowers) if isinstance(knowers, list) else set()
        if secret.get("holder") not in pool_set and not knower_set & pool_set:
            continue
        risk = secret.get("exposure_risk", 0.0)
        if isinstance(risk, (int, float)) and not isinstance(risk, bool):
            risks.append(float(risk))
    return 3.0 * max(risks)


def _pressure_weight(context: EngineContext) -> float:
    weight = 0.0
    morale = context.state.get("morale")
    if _is_number(morale) and morale < LOW_MORALE_THRESHOLD:
        weight += 2.0
    if any(
        isinstance(deadline, int)
        and 0 <= deadline - context.tick <= DEADLINE_PRESSURE_WINDOW_TICKS
        for deadline in context.contract_deadlines
    ):
        weight += 2.0
    configured = context.state.get("budget_warning_line")
    warning_line = configured if configured is not None else DEFAULT_BUDGET_WARNING_LINE
    budget = context.state.get("budget")
    if _is_number(budget) and _is_number(warning_line) and budget < warning_line:
        weight += 1.0
    return weight


def _intervention_weight(
    storylet: StoryletDef, context: EngineContext, pool: list[str]
) -> float:
    pool_set = set(pool)
    incident_ids = {payload.get("id") for payload in _matched_incidents(storylet, context)}
    room_ids = {room.get("id") for room in _candidate_rooms(storylet, context)}
    secret_ids = {
        secret.get("id")
        for secret in context.secrets
        if isinstance(secret, dict)
        and (
            secret.get("holder") in pool_set
            or (
                isinstance(secret.get("knowers"), list)
                and set(secret["knowers"]) & pool_set
            )
        )
    }
    for action in context.director_actions:
        if not isinstance(action, dict):
            continue
        targets = action.get("character_ids", [])
        if (
            action.get("incident_id") in incident_ids
            or action.get("room_id") in room_ids
            or action.get("secret_id") in secret_ids
            or (isinstance(targets, list) and set(targets) & pool_set)
        ):
            return 2.0
    return 0.0


def _time_since_spotlight_weight(context: EngineContext, pool: list[str]) -> float:
    if not pool:
        return 0.0
    gaps = [
        ticks_since_spotlight(context.state, character_id, context.tick)
        for character_id in pool
    ]
    known = [gap for gap in gaps if gap is not None]
    if not known:
        return 2.5
    return min(2.5, max(0, min(known)) * 0.25)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --- draw -----------------------------------------------------------------------------


def select_storylet(
    registry: StoryletRegistry,
    *,
    context: EngineContext,
    seed: int,
    log: RollLog | None = None,
) -> StoryletSelection | None:
    """Draw one spotlight storylet and its cast. `None` when nothing is eligible."""
    eligible = eligible_storylets(registry, context=context)
    if not eligible:
        return None
    # Sorting here, not inside `weighted_choice`, keeps the primitive order-trusting while
    # making selection independent of registry insertion order.
    scored = [
        (storylet.id, salience_score(storylet, context=context))
        for storylet in sorted(eligible, key=lambda storylet: storylet.id)
    ]
    rng = RngStream(seed=seed, stream=SELECT_STREAM, tick=context.tick, log=log)
    chosen_id = rng.weighted_choice("storylet", scored)
    storylet = registry.storylets[chosen_id]
    participants = _draw_participants(storylet, context=context, seed=seed, log=log)
    return StoryletSelection(
        storylet=storylet,
        score=dict(scored)[chosen_id],
        participants=participants,
        decision_bindings=_bind_decision_points(storylet, participants),
    )


def _draw_participants(
    storylet: StoryletDef,
    *,
    context: EngineContext,
    seed: int,
    log: RollLog | None,
) -> dict[str, list[str]]:
    rng = RngStream(seed=seed, stream=PARTICIPANT_STREAM, tick=context.tick, log=log)
    slot_candidates = _slot_candidate_lists(storylet, context)
    assigned: list[str] = []
    participants: dict[str, list[str]] = {}
    for slot in storylet.participants:
        candidates = slot_candidates[slot.slot]
        available = [c for c in candidates if c not in assigned]
        if not available and slot.required:
            available = list(candidates)
        preferred = sorted(
            c for c in available if _satisfies_quality_preconditions(storylet, context, c)
        )
        fallback = sorted(c for c in available if c not in preferred)
        picks: list[str] = []
        for _ in range(min(slot.max_count, len(available))):
            bucket = preferred if preferred else fallback
            pick = rng.weighted_choice(
                f"{storylet.id}:{slot.slot}", [(c, 1.0) for c in bucket]
            )
            bucket.remove(pick)
            picks.append(pick)
        participants[slot.slot] = picks
        assigned.extend(picks)
    return participants


def _bind_decision_points(
    storylet: StoryletDef, participants: dict[str, list[str]]
) -> tuple[dict[str, Any], ...]:
    """Bind decision-point ids to drawn characters. Running them is #13's seam."""
    bindings: list[dict[str, Any]] = []
    for point in storylet.decision_points:
        cast = participants.get(point.character_slot, [])
        bindings.append(
            {
                "id": point.id,
                "decision_type": point.decision_type,
                "character_slot": point.character_slot,
                "character_id": cast[0] if cast else None,
            }
        )
    return tuple(bindings)
