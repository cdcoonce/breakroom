import json
from pathlib import Path

import pytest

from breakroom.init import init_world
from breakroom.worldstate import (
    ValidationError,
    apply_event,
    character_edges,
    character_qualities,
    diff_states,
    edge_provenance,
    edge_qualities,
    edges_at_or_above,
    load_snapshot,
    load_world,
    replay_events,
    ticks_since_spotlight,
    write_snapshot,
)


def test_load_world_validates_character_fields_precisely(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    (world / "characters" / "jordan-vale.toml").write_text(
        'id = "jordan-vale"\nname = "Jordan Vale"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="characters/jordan-vale.toml: missing model"):
        load_world(world)


def test_load_world_validates_tower_fields_precisely(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    state = json.loads((world / "state" / "tower.json").read_text())
    state.pop("rooms")
    (world / "state" / "tower.json").write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="state/tower.json: missing rooms"):
        load_world(world)


def test_load_world_rejects_non_int_scalar_field(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    state = json.loads((world / "state" / "tower.json").read_text())
    state["morale"] = "50"
    (world / "state" / "tower.json").write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="state/tower.json: morale must be an int"):
        load_world(world)


def test_replaying_event_log_reproduces_current_state(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    initial = load_world(world).state
    incident = {
        "sequence": 1,
        "type": "incident",
        "day": 1,
        "incident": {"id": "coffee-spill", "morale_delta": -2},
    }
    scene = {
        "sequence": 2,
        "type": "scene",
        "day": 1,
        "character_id": "jordan-vale",
        "incident_id": "coffee-spill",
    }
    (world / "events.jsonl").write_text(
        json.dumps(incident, sort_keys=True) + "\n" + json.dumps(scene, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    current = apply_event(apply_event(initial, incident), scene)
    (world / "state" / "tower.json").write_text(
        json.dumps(current, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert replay_events(initial, world / "events.jsonl") == load_world(world).state


def test_snapshot_write_load_compare_is_lossless(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    loaded = load_world(world)

    snapshot = write_snapshot(world, loaded.state, name="day-0000")

    assert load_snapshot(snapshot) == loaded.state
    assert diff_states(loaded.state, load_snapshot(snapshot)) == {}


def test_independent_event_application_is_order_stable_and_pure() -> None:
    state = {
        "seed": 42,
        "day": 0,
        "budget": 1000,
        "morale": 50,
        "reputation": 50,
        "rooms": [],
        "characters": [],
    }
    budget_event = {"type": "dial_delta", "day": 1, "dials": {"budget": -100}}
    reputation_event = {"type": "dial_delta", "day": 1, "dials": {"reputation": 3}}

    first = apply_event(apply_event(state, budget_event), reputation_event)
    second = apply_event(apply_event(state, reputation_event), budget_event)

    assert first == second
    assert state["budget"] == 1000
    assert state["reputation"] == 50


def test_dial_delta_with_unknown_dial_is_rejected() -> None:
    state = {
        "seed": 42,
        "day": 0,
        "budget": 1000,
        "morale": 50,
        "reputation": 50,
        "rooms": [],
        "characters": [],
    }
    event = {"type": "dial_delta", "day": 1, "dials": {"unknown_dial": 1}}

    with pytest.raises(ValidationError, match="unknown_dial"):
        apply_event(state, event)


def test_edge_delta_without_event_id_is_rejected() -> None:
    state = {"day": 0}
    event = {
        "type": "edge_delta",
        "day": 1,
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": 1}},
    }

    with pytest.raises(ValidationError, match="edge_delta event: missing event_id"):
        apply_event(state, event)


def test_edge_delta_missing_pair_or_edges_fields_is_rejected() -> None:
    state = {"day": 0}
    base = {"type": "edge_delta", "day": 1, "event_id": "evt-1"}

    with pytest.raises(ValidationError, match="edge_delta event: missing from"):
        apply_event(state, {**base, "to": "sam-oduya", "edges": {}})
    with pytest.raises(ValidationError, match="edge_delta event: missing to"):
        apply_event(state, {**base, "from": "jordan-vale", "edges": {}})
    with pytest.raises(ValidationError, match="edge_delta event: missing edges"):
        apply_event(state, {**base, "from": "jordan-vale", "to": "sam-oduya"})


def test_edge_delta_applies_typed_quality_queryable_by_pair() -> None:
    state = {"day": 0}
    event = {
        "type": "edge_delta",
        "day": 1,
        "event_id": "evt-1",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": 2}, "fear": {"delta": -1}},
    }

    next_state = apply_event(state, event)

    qualities = edge_qualities(next_state, "jordan-vale", "sam-oduya")
    assert qualities["trust"]["value"] == 2
    assert qualities["fear"]["value"] == -1
    assert "edges" not in state


def test_edges_key_absent_from_raw_state_reads_as_empty() -> None:
    state = {"day": 0}

    assert edge_qualities(state, "jordan-vale", "sam-oduya") == {}
    assert character_edges(state, "jordan-vale") == []
    assert edges_at_or_above(state, "trust", 1) == []
    assert edge_provenance(state, "jordan-vale", "sam-oduya", "trust") == []


def test_edge_provenance_walks_full_causing_event_list() -> None:
    state = {"day": 0}
    first = {
        "type": "edge_delta",
        "day": 1,
        "event_id": "evt-1",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": 1}},
    }
    second = {
        "type": "edge_delta",
        "day": 2,
        "event_id": "evt-2",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": 2}},
    }

    next_state = apply_event(apply_event(state, first), second)

    assert edge_provenance(next_state, "jordan-vale", "sam-oduya", "trust") == ["evt-1", "evt-2"]
    assert edge_qualities(next_state, "jordan-vale", "sam-oduya")["trust"]["value"] == 3


def test_character_edges_finds_pairs_in_either_direction() -> None:
    state = {"day": 0}
    outgoing = {
        "type": "edge_delta",
        "day": 1,
        "event_id": "evt-1",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": 1}},
    }
    incoming = {
        "type": "edge_delta",
        "day": 1,
        "event_id": "evt-2",
        "from": "priya-nair",
        "to": "jordan-vale",
        "edges": {"rivalry": {"delta": 2}},
    }

    next_state = apply_event(apply_event(state, outgoing), incoming)

    pairs = {(edge["from"], edge["to"]) for edge in character_edges(next_state, "jordan-vale")}
    assert pairs == {("jordan-vale", "sam-oduya"), ("priya-nair", "jordan-vale")}


def test_edges_at_or_above_filters_by_quality_threshold() -> None:
    state = {"day": 0}
    low = {
        "type": "edge_delta",
        "day": 1,
        "event_id": "evt-1",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": 1}},
    }
    high = {
        "type": "edge_delta",
        "day": 1,
        "event_id": "evt-2",
        "from": "priya-nair",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": 4}},
    }

    next_state = apply_event(apply_event(state, low), high)

    hits = edges_at_or_above(next_state, "trust", 3)
    assert hits == [{"from": "priya-nair", "to": "sam-oduya", "value": 4}]


def test_permanent_cap_scar_caps_recovery_under_later_positive_delta() -> None:
    state = {"day": 0}
    scar = {
        "type": "edge_delta",
        "day": 1,
        "event_id": "evt-scar",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": -4, "cap": -2}},
    }
    recovery = {
        "type": "edge_delta",
        "day": 2,
        "event_id": "evt-recovery",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": 10}},
    }

    scarred = apply_event(state, scar)
    assert edge_qualities(scarred, "jordan-vale", "sam-oduya")["trust"]["value"] == -4

    recovered = apply_event(scarred, recovery)
    assert edge_qualities(recovered, "jordan-vale", "sam-oduya")["trust"]["value"] == -2


def test_strictest_permanent_cap_wins_and_a_later_scar_cannot_loosen_it() -> None:
    state = {"day": 0}
    strict_scar = {
        "type": "edge_delta",
        "day": 1,
        "event_id": "evt-strict",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": -5, "cap": -5}},
    }
    looser_scar = {
        "type": "edge_delta",
        "day": 2,
        "event_id": "evt-looser",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": 8, "cap": 1}},
    }

    state = apply_event(state, strict_scar)
    state = apply_event(state, looser_scar)

    qualities = edge_qualities(state, "jordan-vale", "sam-oduya")
    assert qualities["trust"]["cap"] == -5
    assert qualities["trust"]["value"] == -5


def test_permanent_floor_scar_holds_recovery_under_later_negative_delta() -> None:
    state = {"day": 0}
    scar = {
        "type": "edge_delta",
        "day": 1,
        "event_id": "evt-scar",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"fear": {"delta": 6, "floor": 6}},
    }
    harm = {
        "type": "edge_delta",
        "day": 2,
        "event_id": "evt-harm",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"fear": {"delta": -10}},
    }

    scarred = apply_event(state, scar)
    assert edge_qualities(scarred, "jordan-vale", "sam-oduya")["fear"]["value"] == 6

    harmed = apply_event(scarred, harm)
    assert edge_qualities(harmed, "jordan-vale", "sam-oduya")["fear"]["value"] == 6


def test_strictest_permanent_floor_wins_and_a_later_scar_cannot_loosen_it() -> None:
    state = {"day": 0}
    strict_scar = {
        "type": "edge_delta",
        "day": 1,
        "event_id": "evt-strict",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"fear": {"delta": 5, "floor": 5}},
    }
    looser_scar = {
        "type": "edge_delta",
        "day": 2,
        "event_id": "evt-looser",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"fear": {"delta": -8, "floor": 1}},
    }

    state = apply_event(state, strict_scar)
    state = apply_event(state, looser_scar)

    qualities = edge_qualities(state, "jordan-vale", "sam-oduya")
    assert qualities["fear"]["floor"] == 5
    assert qualities["fear"]["value"] == 5


def test_cap_and_floor_accumulate_independently_and_pin_value_when_equal() -> None:
    state = {"day": 0}
    cap_scar = {
        "type": "edge_delta",
        "day": 1,
        "event_id": "evt-cap",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"rivalry": {"delta": 4, "cap": 4}},
    }
    floor_scar = {
        "type": "edge_delta",
        "day": 2,
        "event_id": "evt-floor",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"rivalry": {"delta": -1, "floor": 4}},
    }
    push_up = {
        "type": "edge_delta",
        "day": 3,
        "event_id": "evt-push-up",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"rivalry": {"delta": 3}},
    }
    push_down = {
        "type": "edge_delta",
        "day": 4,
        "event_id": "evt-push-down",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"rivalry": {"delta": -3}},
    }

    state = apply_event(state, cap_scar)
    state = apply_event(state, floor_scar)
    qualities = edge_qualities(state, "jordan-vale", "sam-oduya")
    assert qualities["rivalry"]["cap"] == 4
    assert qualities["rivalry"]["floor"] == 4
    assert qualities["rivalry"]["value"] == 4

    state = apply_event(state, push_up)
    assert edge_qualities(state, "jordan-vale", "sam-oduya")["rivalry"]["value"] == 4

    state = apply_event(state, push_down)
    assert edge_qualities(state, "jordan-vale", "sam-oduya")["rivalry"]["value"] == 4


def test_single_change_with_floor_above_cap_is_rejected() -> None:
    state = {"day": 0}
    event = {
        "type": "edge_delta",
        "day": 1,
        "event_id": "evt-contradiction",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": 1, "cap": 2, "floor": 3}},
    }

    with pytest.raises(ValidationError, match="floor 3 exceeds cap 2"):
        apply_event(state, event)


def test_new_floor_contradicting_an_earlier_accumulated_cap_is_rejected() -> None:
    state = {"day": 0}
    cap_scar = {
        "type": "edge_delta",
        "day": 1,
        "event_id": "evt-cap",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": -3, "cap": -3}},
    }
    contradicting_floor = {
        "type": "edge_delta",
        "day": 2,
        "event_id": "evt-floor",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": 0, "floor": 0}},
    }

    state = apply_event(state, cap_scar)

    with pytest.raises(ValidationError, match="floor 0 exceeds cap -3"):
        apply_event(state, contradicting_floor)


def test_scene_event_with_character_ids_records_spotlight_history_per_character() -> None:
    state = {"day": 0}
    scene = {
        "type": "scene",
        "day": 3,
        "storylet_id": "coffee-spill-fallout",
        "character_ids": ["jordan-vale", "sam-oduya"],
    }

    next_state = apply_event(state, scene)

    assert next_state["spotlight_history"]["jordan-vale"] == 3
    assert next_state["spotlight_history"]["sam-oduya"] == 3

    later_scene = {
        "type": "scene",
        "day": 5,
        "storylet_id": "solo-follow-up",
        "character_ids": ["jordan-vale"],
    }
    advanced = apply_event(next_state, later_scene)

    assert advanced["spotlight_history"]["jordan-vale"] == 5
    assert advanced["spotlight_history"]["sam-oduya"] == 3


def test_legacy_scene_event_leaves_spotlight_history_untouched(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    initial = load_world(world).state
    incident = {
        "sequence": 1,
        "type": "incident",
        "day": 1,
        "incident": {"id": "coffee-spill", "morale_delta": -2},
    }
    scene = {
        "sequence": 2,
        "type": "scene",
        "day": 1,
        "character_id": "jordan-vale",
        "incident_id": "coffee-spill",
    }
    (world / "events.jsonl").write_text(
        json.dumps(incident, sort_keys=True) + "\n" + json.dumps(scene, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    current = apply_event(apply_event(initial, incident), scene)
    (world / "state" / "tower.json").write_text(
        json.dumps(current, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    replayed = replay_events(initial, world / "events.jsonl")

    assert replayed == load_world(world).state
    assert "spotlight_history" not in replayed
    assert replayed["day"] == 1


def test_scene_character_ids_present_but_empty_is_rejected() -> None:
    state = {"day": 0}
    event = {
        "type": "scene",
        "day": 1,
        "storylet_id": "solo",
        "character_ids": [],
    }

    with pytest.raises(ValidationError, match="character_ids"):
        apply_event(state, event)


def test_scene_character_ids_non_list_is_rejected() -> None:
    state = {"day": 0}
    event = {
        "type": "scene",
        "day": 1,
        "storylet_id": "solo",
        "character_ids": "jordan-vale",
    }

    with pytest.raises(ValidationError, match="character_ids"):
        apply_event(state, event)


def test_scene_character_ids_containing_non_strings_is_rejected() -> None:
    state = {"day": 0}
    event = {
        "type": "scene",
        "day": 1,
        "storylet_id": "solo",
        "character_ids": ["jordan-vale", 7],
    }

    with pytest.raises(ValidationError, match="character_ids"):
        apply_event(state, event)


def test_scene_character_ids_without_storylet_id_is_rejected() -> None:
    state = {"day": 0}
    event = {
        "type": "scene",
        "day": 1,
        "character_ids": ["jordan-vale"],
    }

    with pytest.raises(ValidationError, match="storylet_id"):
        apply_event(state, event)


def test_ticks_since_spotlight_returns_none_for_never_spotlighted_character() -> None:
    state = {"day": 0}

    assert ticks_since_spotlight(state, "jordan-vale", current_day=10) is None


def test_ticks_since_spotlight_returns_days_since_last_spotlight() -> None:
    state = {"day": 0}
    scene = {
        "type": "scene",
        "day": 3,
        "storylet_id": "coffee-spill-fallout",
        "character_ids": ["jordan-vale"],
    }

    next_state = apply_event(state, scene)

    assert ticks_since_spotlight(next_state, "jordan-vale", current_day=10) == 7


def _character_toml(qualities_line: str) -> str:
    return (
        'id = "jordan-vale"\n'
        'name = "Jordan Vale"\n'
        'model = "claude-3-5-haiku"\n'
        f"{qualities_line}\n"
        "\n"
        "[stats]\n"
        "focus = 2\n"
        "empathy = 3\n"
        "nerve = 2\n"
    )


def _write_character_qualities(world: Path, qualities_line: str) -> None:
    (world / "characters" / "jordan-vale.toml").write_text(
        _character_toml(qualities_line), encoding="utf-8"
    )


def test_character_quality_bad_namespace_is_rejected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_character_qualities(world, 'qualities = { "foo:x" = true }')

    with pytest.raises(ValidationError, match="foo:x"):
        load_world(world)


def test_character_quality_unnamespaced_key_is_rejected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_character_qualities(world, 'qualities = { "new-hire" = true }')

    with pytest.raises(ValidationError, match="new-hire"):
        load_world(world)


def test_character_quality_stat_namespace_is_rejected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_character_qualities(world, 'qualities = { "stat:focus" = true }')

    with pytest.raises(ValidationError, match="stat:focus"):
        load_world(world)


def test_character_quality_rel_namespace_is_rejected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_character_qualities(world, 'qualities = { "rel:rivalry" = true }')

    with pytest.raises(ValidationError, match="rel:rivalry"):
        load_world(world)


def test_character_quality_room_namespace_is_rejected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_character_qualities(world, 'qualities = { "room:break-room" = true }')

    with pytest.raises(ValidationError, match="room:break-room"):
        load_world(world)


def test_character_quality_scalar_above_range_is_rejected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_character_qualities(world, 'qualities = { "trait:x" = 4 }')

    with pytest.raises(ValidationError, match="trait:x"):
        load_world(world)


def test_character_quality_scalar_below_range_is_rejected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_character_qualities(world, 'qualities = { "trait:x" = -4 }')

    with pytest.raises(ValidationError, match="trait:x"):
        load_world(world)


def test_character_quality_false_value_is_rejected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_character_qualities(world, 'qualities = { "trait:x" = false }')

    with pytest.raises(ValidationError, match="trait:x"):
        load_world(world)


def test_character_quality_string_value_is_rejected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_character_qualities(world, 'qualities = { "trait:x" = "high" }')

    with pytest.raises(ValidationError, match="trait:x"):
        load_world(world)


def test_character_quality_float_value_is_rejected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_character_qualities(world, 'qualities = { "trait:x" = 2.5 }')

    with pytest.raises(ValidationError, match="trait:x"):
        load_world(world)


def test_character_quality_true_and_one_are_distinct_legal_values(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_character_qualities(
        world, 'qualities = { "trait:boolean-quality" = true, "trait:scalar-quality" = 1 }'
    )

    loaded = load_world(world)

    qualities = character_qualities(loaded.characters, "jordan-vale")
    assert qualities["trait:boolean-quality"] is True
    assert type(qualities["trait:scalar-quality"]) is int
    assert qualities["trait:scalar-quality"] == 1
    assert qualities["trait:scalar-quality"] is not True


def test_character_qualities_absent_field_defaults_to_empty_dict() -> None:
    characters = {"jordan-vale": {"id": "jordan-vale"}}

    assert character_qualities(characters, "jordan-vale") == {}


def test_character_qualities_missing_character_defaults_to_empty_dict() -> None:
    characters: dict[str, dict[str, object]] = {}

    assert character_qualities(characters, "jordan-vale") == {}


def test_character_qualities_returned_dict_does_not_alias_stored_state() -> None:
    characters = {"jordan-vale": {"qualities": {"trait:people-pleaser": True}}}

    result = character_qualities(characters, "jordan-vale")
    result["trait:new-hire"] = True
    del result["trait:people-pleaser"]

    assert characters["jordan-vale"]["qualities"] == {"trait:people-pleaser": True}


def test_init_world_starter_character_qualities_round_trip(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)

    loaded = load_world(world)

    assert character_qualities(loaded.characters, "jordan-vale") == {
        "state:new-hire": True,
        "trait:people-pleaser": True,
    }


def test_character_qualities_of_wrong_shape_is_rejected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_character_qualities(world, 'qualities = ["new-hire", "people-pleaser"]')

    with pytest.raises(ValidationError, match="qualities must be a table"):
        load_world(world)


def test_character_quality_empty_name_is_rejected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_character_qualities(world, 'qualities = { "trait:" = true }')

    with pytest.raises(ValidationError, match="trait:"):
        load_world(world)
