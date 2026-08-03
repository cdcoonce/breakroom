import json
from pathlib import Path

import pytest

from breakroom.init import init_world
from breakroom.worldstate import (
    ValidationError,
    apply_event,
    diff_states,
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
