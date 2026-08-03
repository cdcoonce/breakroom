import json
from pathlib import Path

import pytest

from breakroom.init import init_world
from breakroom.worldstate import (
    ValidationError,
    apply_event,
    character_edges,
    diff_states,
    edge_provenance,
    edge_qualities,
    edges_at_or_above,
    load_snapshot,
    load_world,
    replay_events,
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


def test_permanent_floor_scar_caps_recovery_under_later_positive_delta() -> None:
    state = {"day": 0}
    scar = {
        "type": "edge_delta",
        "day": 1,
        "event_id": "evt-scar",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": -4, "floor": -2}},
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


def test_strictest_permanent_floor_wins_and_a_later_scar_cannot_loosen_it() -> None:
    state = {"day": 0}
    strict_scar = {
        "type": "edge_delta",
        "day": 1,
        "event_id": "evt-strict",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": -5, "floor": -5}},
    }
    looser_scar = {
        "type": "edge_delta",
        "day": 2,
        "event_id": "evt-looser",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {"trust": {"delta": 8, "floor": 1}},
    }

    state = apply_event(state, strict_scar)
    state = apply_event(state, looser_scar)

    qualities = edge_qualities(state, "jordan-vale", "sam-oduya")
    assert qualities["trust"]["floor"] == -5
    assert qualities["trust"]["value"] == -5
