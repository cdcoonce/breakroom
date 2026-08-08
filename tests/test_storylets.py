from pathlib import Path

import pytest

from breakroom.secrets import NEAR_EXPOSURE_THRESHOLD
from breakroom.storylets import (
    SALIENCE_FLOOR,
    EngineContext,
    StoryletRegistry,
    eligible_storylets,
    load_registry,
    salience_score,
    select_storylet,
)
from breakroom.worldstate import ValidationError

VALID_STORYLET = """\
id = "shared-space-repair"
title = "Shared Space Repair"
premise = "A mess in a common room tests whether anyone treats shared space as shared."
kind = "incident_response"

[eligibility]
incident_ids = ["coffee-spill"]
room_kinds = ["social"]
required_quality_any = [
  "trait:people-pleaser",
  { quality = "trait:tidy", operator = "gte", value = 2 },
]
forbidden_state_any = ["state:offstage"]
min_tick_gap = 3

[[participants]]
slot = "responder"
source = "incident.cleanup_owner"
required = true

[[participants]]
slot = "witness"
source = "same_room"
required = false
max_count = 2

[[decision_points]]
id = "cleanup_choice"
decision_type = "incident_response"
character_slot = "responder"

[[effect_hooks]]
hook = "incident_cleanup_resolution"
when = "after_decision"

[salience]
storylet_bias = 0.5
"""

MINIMAL_STORYLET = """\
id = "quiet-room"
title = "Quiet Room"
premise = "A room goes quiet and someone decides whether to bridge the gap."
kind = "ambient"

[eligibility]
room_kinds = ["social"]

[[participants]]
slot = "occupant"
source = "same_room"
required = true
max_count = 2
"""


def _write_storylets(world: Path, files: dict[str, str]) -> Path:
    directory = world / "data" / "storylets"
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / f"{name}.toml").write_text(text, encoding="utf-8")
    return world


def _tower_state(**overrides: object) -> dict:
    state: dict = {
        "seed": 7,
        "day": 12,
        "budget": 1000,
        "morale": 50,
        "reputation": 50,
        "rooms": [
            {"id": "break-room", "name": "Break Room", "kind": "social", "floor": 1},
            {"id": "open-office", "name": "Open Office", "kind": "work", "floor": 2},
        ],
        "characters": ["jordan-vale", "mira-okonkwo"],
        "edges": {},
        "spotlight_history": {},
    }
    state.update(overrides)
    return state


def _incident_event(day: int, incident_id: str, **payload: object) -> dict:
    return {
        "type": "incident",
        "day": day,
        "incident": {"id": incident_id, "room": "break-room", **payload},
    }


# --- registry -------------------------------------------------------------------------


def test_load_registry_parses_valid_storylet(tmp_path: Path) -> None:
    world = _write_storylets(tmp_path / "tower", {"shared-space-repair": VALID_STORYLET})

    registry = load_registry(world)

    storylet = registry.storylets["shared-space-repair"]
    assert storylet.kind == "incident_response"
    assert storylet.eligibility.incident_ids == ("coffee-spill",)
    assert storylet.eligibility.min_tick_gap == 3
    assert [slot.slot for slot in storylet.participants] == ["responder", "witness"]
    assert storylet.participants[1].max_count == 2
    assert storylet.decision_points[0].id == "cleanup_choice"
    assert storylet.effect_hooks[0].when == "after_decision"
    assert storylet.storylet_bias == 0.5


def test_load_registry_rejects_unknown_storylet_field(tmp_path: Path) -> None:
    text = VALID_STORYLET.replace(
        'kind = "incident_response"', 'kind = "incident_response"\nnarrator_hint = "loud"'
    )
    world = _write_storylets(tmp_path / "tower", {"broken": text})

    with pytest.raises(ValidationError, match="unknown storylet fields"):
        load_registry(world)


def test_load_registry_rejects_unknown_precondition_field(tmp_path: Path) -> None:
    text = VALID_STORYLET.replace("min_tick_gap = 3", 'weather = "rain"')
    world = _write_storylets(tmp_path / "tower", {"broken": text})

    with pytest.raises(ValidationError, match="unknown precondition fields"):
        load_registry(world)


def test_load_registry_rejects_unknown_operator(tmp_path: Path) -> None:
    text = VALID_STORYLET.replace('operator = "gte"', 'operator = "neq"')
    world = _write_storylets(tmp_path / "tower", {"broken": text})

    with pytest.raises(ValidationError, match="invalid quality operator"):
        load_registry(world)


def test_load_registry_rejects_unknown_participant_source(tmp_path: Path) -> None:
    text = VALID_STORYLET.replace('source = "same_room"', 'source = "same_elevator"')
    world = _write_storylets(tmp_path / "tower", {"broken": text})

    with pytest.raises(ValidationError, match="invalid participant source"):
        load_registry(world)


def test_load_registry_rejects_out_of_range_storylet_bias(tmp_path: Path) -> None:
    text = VALID_STORYLET.replace("storylet_bias = 0.5", "storylet_bias = 4.0")
    world = _write_storylets(tmp_path / "tower", {"broken": text})

    with pytest.raises(ValidationError, match="storylet_bias"):
        load_registry(world)


def test_load_registry_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="missing directory"):
        load_registry(tmp_path / "tower")


# --- eligibility ----------------------------------------------------------------------


def _incident_context(**overrides: object) -> EngineContext:
    defaults: dict = {
        "tick": 12,
        "state": _tower_state(),
        "characters": {
            "jordan-vale": {"id": "jordan-vale", "qualities": {"trait:people-pleaser": True}},
            "mira-okonkwo": {"id": "mira-okonkwo", "qualities": {}},
        },
        "character_rooms": {"jordan-vale": "break-room", "mira-okonkwo": "break-room"},
        "incident_events": [
            _incident_event(12, "coffee-spill", cleanup_owner="jordan-vale", resolved=False)
        ],
    }
    defaults.update(overrides)
    return EngineContext(**defaults)  # type: ignore[arg-type]


def _eligible_ids(world: Path, context: EngineContext) -> list[str]:
    registry = load_registry(world)
    return [storylet.id for storylet in eligible_storylets(registry, context=context)]


def test_incident_ids_precondition_gates_on_current_tick_incident(tmp_path: Path) -> None:
    world = _write_storylets(tmp_path / "tower", {"shared-space-repair": VALID_STORYLET})

    assert _eligible_ids(world, _incident_context()) == ["shared-space-repair"]

    other = _incident_context(
        incident_events=[
            _incident_event(12, "printer-jam", cleanup_owner="jordan-vale", resolved=False)
        ]
    )
    assert _eligible_ids(world, other) == []


def test_room_kinds_precondition_gates_on_incident_room_kind(tmp_path: Path) -> None:
    world = _write_storylets(tmp_path / "tower", {"shared-space-repair": VALID_STORYLET})

    state = _tower_state(
        rooms=[{"id": "break-room", "name": "Break Room", "kind": "work", "floor": 1}]
    )
    assert _eligible_ids(world, _incident_context(state=state)) == []


def test_required_quality_any_precondition_needs_a_castable_character(tmp_path: Path) -> None:
    world = _write_storylets(tmp_path / "tower", {"shared-space-repair": VALID_STORYLET})

    context = _incident_context(
        characters={
            "jordan-vale": {"id": "jordan-vale", "qualities": {"trait:tidy": 1}},
            "mira-okonkwo": {"id": "mira-okonkwo", "qualities": {}},
        }
    )
    assert _eligible_ids(world, context) == []


def test_forbidden_state_any_precondition_needs_an_unblocked_character(tmp_path: Path) -> None:
    world = _write_storylets(tmp_path / "tower", {"shared-space-repair": VALID_STORYLET})

    blocked = {
        "jordan-vale": {
            "id": "jordan-vale",
            "qualities": {"trait:people-pleaser": True, "state:offstage": True},
        },
        "mira-okonkwo": {"id": "mira-okonkwo", "qualities": {"state:offstage": True}},
    }
    assert _eligible_ids(world, _incident_context(characters=blocked)) == []


def test_min_tick_gap_precondition_blocks_a_recent_repeat(tmp_path: Path) -> None:
    world = _write_storylets(tmp_path / "tower", {"shared-space-repair": VALID_STORYLET})

    too_soon = _incident_context(storylet_history={"shared-space-repair": 10})
    assert _eligible_ids(world, too_soon) == []

    long_enough = _incident_context(storylet_history={"shared-space-repair": 9})
    assert _eligible_ids(world, long_enough) == ["shared-space-repair"]


def test_candidate_pool_quality_semantics_need_exactly_one_qualifier(tmp_path: Path) -> None:
    """The pool is the union of every slot's raw candidates; one qualifier is enough."""
    world = _write_storylets(tmp_path / "tower", {"shared-space-repair": VALID_STORYLET})

    # `mira-okonkwo` reaches the pool only through the optional `same_room` witness slot.
    one_qualifier = _incident_context(
        characters={
            "jordan-vale": {"id": "jordan-vale", "qualities": {}},
            "mira-okonkwo": {"id": "mira-okonkwo", "qualities": {"trait:tidy": 2}},
        }
    )
    assert _eligible_ids(world, one_qualifier) == ["shared-space-repair"]

    none_qualify = _incident_context(
        characters={
            "jordan-vale": {"id": "jordan-vale", "qualities": {}},
            "mira-okonkwo": {"id": "mira-okonkwo", "qualities": {"trait:tidy": 1}},
        }
    )
    assert _eligible_ids(world, none_qualify) == []


def test_required_slot_that_cannot_be_filled_is_ineligible(tmp_path: Path) -> None:
    world = _write_storylets(tmp_path / "tower", {"shared-space-repair": VALID_STORYLET})

    context = _incident_context(
        incident_events=[_incident_event(12, "coffee-spill", cleanup_owner=None, resolved=False)]
    )
    assert _eligible_ids(world, context) == []


def test_secret_knower_source_uses_the_near_exposure_threshold(tmp_path: Path) -> None:
    text = MINIMAL_STORYLET.replace('source = "same_room"', 'source = "secret_knower"')
    world = _write_storylets(tmp_path / "tower", {"quiet-room": text})

    near = {
        "id": "budget-leak",
        "holder": "jordan-vale",
        "exposure_risk": NEAR_EXPOSURE_THRESHOLD,
        "knowers": ["jordan-vale"],
        "state": "sealed",
    }
    assert _eligible_ids(world, _incident_context(secrets=[near])) == ["quiet-room"]

    far = {**near, "exposure_risk": NEAR_EXPOSURE_THRESHOLD - 0.1}
    assert _eligible_ids(world, _incident_context(secrets=[far])) == []


# --- salience -------------------------------------------------------------------------

WORKED_EXAMPLE_STORYLET = """\
id = "shared-space-repair"
title = "Shared Space Repair"
premise = "A mess in a common room tests whether anyone treats shared space as shared."
kind = "incident_response"

[eligibility]
incident_ids = ["coffee-spill"]

[[participants]]
slot = "responder"
source = "incident.cleanup_owner"
required = true

[[decision_points]]
id = "cleanup_choice"
decision_type = "incident_response"
character_slot = "responder"

[salience]
storylet_bias = 0.5
"""


def _worked_example_context() -> EngineContext:
    state = _tower_state(
        budget=100,
        morale=10,
        spotlight_history={"jordan-vale": 8},
    )
    return EngineContext(
        tick=12,
        state=state,
        characters={"jordan-vale": {"id": "jordan-vale", "qualities": {}}},
        character_rooms={"jordan-vale": "break-room"},
        incident_events=[
            _incident_event(12, "coffee-spill", cleanup_owner="jordan-vale", resolved=False)
        ],
        secrets=[
            {
                "id": "budget-leak",
                "holder": "jordan-vale",
                "exposure_risk": 0.5,
                "knowers": ["jordan-vale"],
                "state": "sealed",
            }
        ],
        edge_delta_events=[
            {
                "type": "edge_delta",
                "day": 10,
                "from": "jordan-vale",
                "to": "mira-okonkwo",
                "edges": {"rel:trust": {"delta": 2}},
            },
            {
                "type": "edge_delta",
                "day": 11,
                "from": "mira-okonkwo",
                "to": "jordan-vale",
                "edges": {"rel:rivalry": {"delta": -1}},
            },
        ],
        director_actions=[{"id": "nudge-cleanup", "incident_id": "coffee-spill"}],
        contract_deadlines=[13],
    )


def test_salience_worked_example_hot_incident_tick(tmp_path: Path) -> None:
    """base 1.0 + fresh 4.0 + hot_edge 1.5 + exposure 1.5 + pressure 5.0
    + intervention 2.0 + spotlight 1.0 + bias 0.5 = 16.5."""
    world = _write_storylets(
        tmp_path / "tower", {"shared-space-repair": WORKED_EXAMPLE_STORYLET}
    )
    storylet = load_registry(world).storylets["shared-space-repair"]

    assert salience_score(storylet, context=_worked_example_context()) == 16.5


QUIET_WORKED_EXAMPLE = """\
id = "quiet-room"
title = "Quiet Room"
premise = "A room goes quiet and someone decides whether to bridge the gap."
kind = "ambient"

[eligibility]
room_kinds = ["social"]

[[participants]]
slot = "occupant"
source = "same_room"
required = true
max_count = 2

[salience]
storylet_bias = -0.5
"""


def test_salience_worked_example_quiet_tick(tmp_path: Path) -> None:
    """base 1.0 + exposure 0.75 + spotlight 1.25 + bias -0.5 = 2.5."""
    world = _write_storylets(tmp_path / "tower", {"quiet-room": QUIET_WORKED_EXAMPLE})
    storylet = load_registry(world).storylets["quiet-room"]

    context = EngineContext(
        tick=5,
        state=_tower_state(day=5, spotlight_history={"jordan-vale": 0, "mira-okonkwo": 0}),
        characters={
            "jordan-vale": {"id": "jordan-vale", "qualities": {}},
            "mira-okonkwo": {"id": "mira-okonkwo", "qualities": {}},
        },
        character_rooms={"jordan-vale": "break-room", "mira-okonkwo": "break-room"},
        secrets=[
            {
                "id": "budget-leak",
                "holder": "jordan-vale",
                "exposure_risk": 0.25,
                "knowers": ["jordan-vale"],
                "state": "sealed",
            }
        ],
    )

    assert salience_score(storylet, context=context) == 2.5


def test_salience_worked_example_stale_incident_and_capped_edge_heat(tmp_path: Path) -> None:
    """base 1.0 + fresh 2.0 + hot_edge 3.0 (capped from 4.0) + spotlight 0.25
    + bias 0.5 = 6.75."""
    world = _write_storylets(
        tmp_path / "tower", {"shared-space-repair": WORKED_EXAMPLE_STORYLET}
    )
    storylet = load_registry(world).storylets["shared-space-repair"]

    context = EngineContext(
        tick=20,
        state=_tower_state(day=20, spotlight_history={"jordan-vale": 19}),
        characters={"jordan-vale": {"id": "jordan-vale", "qualities": {}}},
        character_rooms={"jordan-vale": "break-room"},
        incident_events=[
            _incident_event(19, "coffee-spill", cleanup_owner="jordan-vale", resolved=False)
        ],
        edge_delta_events=[
            {
                "type": "edge_delta",
                "day": 18,
                "from": "jordan-vale",
                "to": "mira-okonkwo",
                "edges": {"rel:trust": {"delta": 5}},
            },
            {
                "type": "edge_delta",
                "day": 19,
                "from": "jordan-vale",
                "to": "mira-okonkwo",
                "edges": {"rel:trust": {"delta": -3}},
            },
        ],
    )

    assert salience_score(storylet, context=context) == 6.75


def test_salience_floors_at_the_minimum_drawable_weight(tmp_path: Path) -> None:
    text = QUIET_WORKED_EXAMPLE.replace("storylet_bias = -0.5", "storylet_bias = -2.0")
    world = _write_storylets(tmp_path / "tower", {"quiet-room": text})
    storylet = load_registry(world).storylets["quiet-room"]

    # Empty candidate pool zeroes every pool-derived term, so the raw sum is 1.0 - 2.0.
    context = EngineContext(tick=5, state=_tower_state(day=5))

    assert SALIENCE_FLOOR == 0.1
    assert salience_score(storylet, context=context) == 0.1


# --- draw -----------------------------------------------------------------------------

DRAW_STORYLETS = {
    "shared-space-repair": VALID_STORYLET,
    "quiet-room": MINIMAL_STORYLET,
    "stuck-workflow": MINIMAL_STORYLET.replace('id = "quiet-room"', 'id = "stuck-workflow"')
    .replace('title = "Quiet Room"', 'title = "Stuck Workflow"')
    .replace('room_kinds = ["social"]', 'room_kinds = ["social", "work"]'),
}


def _draw_context() -> EngineContext:
    return _incident_context(
        characters={
            "jordan-vale": {
                "id": "jordan-vale",
                "qualities": {"trait:people-pleaser": True},
            },
            "mira-okonkwo": {"id": "mira-okonkwo", "qualities": {"trait:tidy": 3}},
        },
        state=_tower_state(spotlight_history={"jordan-vale": 4, "mira-okonkwo": 11}),
    )


def test_draw_is_reproducible_for_the_same_seed_and_tick(tmp_path: Path) -> None:
    world = _write_storylets(tmp_path / "tower", DRAW_STORYLETS)
    registry = load_registry(world)
    context = _draw_context()

    first = select_storylet(registry, context=context, seed=42)
    second = select_storylet(registry, context=context, seed=42)

    assert first is not None
    assert second is not None
    assert first.storylet.id == second.storylet.id
    assert first.participants == second.participants
    assert first.score == second.score


def test_draw_is_independent_of_registry_insertion_order(tmp_path: Path) -> None:
    world = _write_storylets(tmp_path / "tower", DRAW_STORYLETS)
    registry = load_registry(world)
    reversed_registry = StoryletRegistry(
        storylets=dict(reversed(list(registry.storylets.items())))
    )
    context = _draw_context()

    for seed in range(20):
        forward = select_storylet(registry, context=context, seed=seed)
        backward = select_storylet(reversed_registry, context=context, seed=seed)
        assert forward is not None
        assert backward is not None
        assert forward.storylet.id == backward.storylet.id
        assert forward.participants == backward.participants


def test_draw_binds_decision_points_to_drawn_characters(tmp_path: Path) -> None:
    world = _write_storylets(tmp_path / "tower", {"shared-space-repair": VALID_STORYLET})
    registry = load_registry(world)

    selection = select_storylet(registry, context=_draw_context(), seed=1)

    assert selection is not None
    assert selection.participants["responder"] == ["jordan-vale"]
    assert selection.decision_bindings == (
        {
            "id": "cleanup_choice",
            "decision_type": "incident_response",
            "character_slot": "responder",
            "character_id": "jordan-vale",
        },
    )


def test_draw_prefers_quality_qualified_characters_within_a_slot(tmp_path: Path) -> None:
    world = _write_storylets(tmp_path / "tower", {"shared-space-repair": VALID_STORYLET})
    registry = load_registry(world)

    # Only `mira-okonkwo` satisfies `required_quality_any`, so she takes the witness slot
    # for every seed even though `casey-flores` is an equally valid `same_room` candidate.
    state = _tower_state(characters=["jordan-vale", "mira-okonkwo", "casey-flores"])
    context = _incident_context(
        state=state,
        characters={
            "jordan-vale": {"id": "jordan-vale", "qualities": {"trait:people-pleaser": True}},
            "mira-okonkwo": {"id": "mira-okonkwo", "qualities": {"trait:tidy": 3}},
            "casey-flores": {"id": "casey-flores", "qualities": {}},
        },
        character_rooms={
            "jordan-vale": "break-room",
            "mira-okonkwo": "break-room",
            "casey-flores": "break-room",
        },
    )

    for seed in range(10):
        selection = select_storylet(registry, context=context, seed=seed)
        assert selection is not None
        assert selection.participants["witness"][0] == "mira-okonkwo"


def test_draw_returns_none_when_nothing_is_eligible(tmp_path: Path) -> None:
    world = _write_storylets(tmp_path / "tower", {"shared-space-repair": VALID_STORYLET})
    registry = load_registry(world)

    context = _incident_context(incident_events=[])

    assert select_storylet(registry, context=context, seed=42) is None


def test_draw_records_rolls_on_the_named_streams(tmp_path: Path) -> None:
    from breakroom.resolution.rng import RollLog

    world = _write_storylets(tmp_path / "tower", DRAW_STORYLETS)
    registry = load_registry(world)
    log = RollLog()

    select_storylet(registry, context=_draw_context(), seed=42, log=log)

    streams = [record["stream"] for record in log.records]
    assert streams[0] == "storylet_select"
    assert set(streams[1:]) == {"storylet_participant"}
