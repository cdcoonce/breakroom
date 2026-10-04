import json
import re
from pathlib import Path

import pytest

from breakroom.economy import load_rulebook, resolve_dial_movement
from breakroom.init import init_world
from breakroom.tick import tick_world
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


def test_load_world_rejects_character_id_mismatched_with_filename(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    (world / "characters" / "jordan-vale.toml").write_text(
        'id = "someone-else"\n'
        'name = "Jordan Vale"\n'
        'model = "claude-3-5-haiku"\n'
        "\n"
        "[stats]\n"
        "focus = 2\n"
        "empathy = 3\n"
        "nerve = 2\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValidationError,
        match="characters/jordan-vale.toml: id 'someone-else' does not match 'jordan-vale'",
    ):
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


def test_load_world_reports_malformed_tower_json_with_resolvable_path(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    tower_path = world / "state" / "tower.json"
    tower_path.write_text("{ malformed json", encoding="utf-8")

    with pytest.raises(ValidationError) as exc_info:
        load_world(world)

    reported_path = exc_info.value.args[0].split(":", 1)[0]
    assert (Path.cwd() / reported_path).resolve() == tower_path.resolve()


def test_init_world_refuses_to_reinitialize_existing_world(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    state_before = json.loads((world / "state" / "tower.json").read_text())

    with pytest.raises(ValidationError):
        init_world(world, seed=99)

    state_after = json.loads((world / "state" / "tower.json").read_text())
    assert state_after["day"] == state_before["day"]
    assert state_after == state_before


@pytest.mark.parametrize(
    "failed_relative_path",
    ["data/storylets/quiet-room.toml", "events.jsonl"],
)
def test_init_world_retries_after_late_scaffold_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_relative_path: str
) -> None:
    world = tmp_path / "tower"
    failed_path = world / failed_relative_path
    original_write_text = Path.write_text
    failed = False

    def fail_scaffold_write_once(path: Path, *args, **kwargs):
        nonlocal failed
        if path == failed_path and not failed:
            failed = True
            raise OSError("injected late scaffold failure")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_scaffold_write_once)

    with pytest.raises(OSError, match="injected late scaffold failure"):
        init_world(world, seed=42)

    assert failed
    assert not (world / "state" / "tower.json").exists()
    assert (world / "characters" / "jordan-vale.toml").exists()
    assert (world / "data" / "norms.toml").exists()
    assert (world / "data" / "incidents.toml").exists()
    storylet_directory = world / "data" / "storylets"
    expected_storylets = {
        "shared-space-repair.toml",
        "stuck-workflow.toml",
        "quiet-room.toml",
    }
    written_storylets = {path.name for path in storylet_directory.glob("*.toml")}
    if failed_relative_path.endswith("quiet-room.toml"):
        assert written_storylets == expected_storylets - {"quiet-room.toml"}
    else:
        assert written_storylets == expected_storylets

    init_world(world, seed=42)

    assert (world / "characters" / "jordan-vale.toml").exists()
    assert (world / "data" / "norms.toml").exists()
    assert (world / "data" / "incidents.toml").exists()
    assert {path.name for path in storylet_directory.glob("*.toml")} == expected_storylets
    assert (world / "events.jsonl").read_text(encoding="utf-8") == ""
    state = json.loads((world / "state" / "tower.json").read_text(encoding="utf-8"))
    assert state["seed"] == 42
    assert state["day"] == 0


def test_load_world_rejects_non_int_scalar_field(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    state = json.loads((world / "state" / "tower.json").read_text())
    state["morale"] = "50"
    (world / "state" / "tower.json").write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValidationError, match="state/tower.json: morale must be a finite int or float"
    ):
        load_world(world)


def test_load_world_accepts_fractional_and_out_of_range_finite_dials(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    path = world / "state" / "tower.json"
    state = json.loads(path.read_text())
    state.update(budget=-0.25, morale=101.5, reputation=-2.5)
    path.write_text(json.dumps(state), encoding="utf-8")

    assert load_world(world).state["budget"] == -0.25
    assert load_world(world).state["morale"] == 101.5
    assert load_world(world).state["reputation"] == -2.5


def test_snapshot_round_trip_preserves_fractional_dials(tmp_path: Path) -> None:
    state = {
        "seed": 42,
        "day": 3,
        "budget": 1000.5,
        "morale": 47.5,
        "reputation": 52.25,
        "rooms": [],
        "characters": [],
    }

    snapshot = write_snapshot(tmp_path, state, "fractional-dials")

    loaded = load_snapshot(snapshot)
    assert {key: loaded[key] for key in ("budget", "morale", "reputation")} == {
        key: state[key] for key in ("budget", "morale", "reputation")
    }
    assert "edge_key_encoding" not in state


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("budget", True),
        ("morale", float("nan")),
        ("reputation", float("inf")),
        ("budget", float("-inf")),
        ("seed", 42.0),
        ("day", True),
    ],
)
def test_load_world_rejects_nonfinite_dials_and_noninteger_clock_fields(
    tmp_path: Path, field: str, value
) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    path = world / "state" / "tower.json"
    state = json.loads(path.read_text())
    state[field] = value
    path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValidationError):
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


def test_replaying_persisted_quiet_day_reproduces_tick_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_narrated(brief: dict) -> str:
        raise AssertionError("a quiet day has no scene to narrate")

    monkeypatch.setattr("breakroom.tick.render_scene", fail_if_narrated)
    world = tmp_path / "tower"
    init_world(world, seed=42)
    initial = load_world(world).state
    incidents_path = world / "data" / "incidents.toml"
    incidents_path.write_text(
        re.sub(r"base_rate = [\d.]+", "base_rate = 0.0", incidents_path.read_text()),
        encoding="utf-8",
    )

    tick_world(world)

    persisted_events = [
        json.loads(line) for line in (world / "events.jsonl").read_text().splitlines()
    ]
    quiet_events = [event for event in persisted_events if event["type"] == "quiet_day"]
    persisted_state = load_world(world).state

    assert len(quiet_events) == 1
    assert quiet_events[0]["day"] == persisted_state["day"]
    assert quiet_events[0]["rolls"]
    assert all(record["result"] is False for record in quiet_events[0]["rolls"])
    assert replay_events(initial, world / "events.jsonl") == persisted_state
    assert persisted_state["day"] == initial["day"] + 1
    assert persisted_state["budget"] == initial["budget"] - 1.0
    assert {
        key: value for key, value in persisted_state.items() if key not in {"day", "budget"}
    } == {
        key: value for key, value in initial.items() if key not in {"day", "budget"}
    }


def test_applying_older_quiet_day_does_not_move_day_backwards() -> None:
    state = {"day": 5, "morale": 50, "edges": {}, "spotlight_history": {}}
    quiet_day = {"type": "quiet_day", "day": 3, "rolls": [{"result": False}]}

    assert apply_event(state, quiet_day) == dict(state, edge_key_encoding="json-pair-v1")


def test_applying_unsupported_event_type_still_raises() -> None:
    with pytest.raises(ValidationError, match="event type unsupported: unsupported"):
        apply_event({"day": 0}, {"type": "unsupported", "day": 1})


def test_replay_events_reports_malformed_json_file_and_physical_line(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n{not json}\n", encoding="utf-8")
    checkout_root = Path(__file__).resolve().parents[1]

    with pytest.raises(ValidationError) as error:
        replay_events({"day": 0}, events_path)

    reported_path, _, detail = str(error.value).partition(": ")
    assert not Path(reported_path).is_absolute()
    assert (checkout_root / reported_path).resolve() == events_path.resolve()
    assert detail.startswith("line 2:")


def test_snapshot_write_load_compare_is_lossless(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    loaded = load_world(world)

    snapshot = write_snapshot(world, loaded.state, name="day-0000")

    assert load_snapshot(snapshot) == loaded.state
    assert diff_states(loaded.state, load_snapshot(snapshot)) == {}


@pytest.mark.parametrize(
    "name",
    ["../evil", "nested/name", "nested\\name", "absolute", ".."],
)
def test_write_snapshot_rejects_path_names_before_creating_files(
    tmp_path: Path, name: str
) -> None:
    world = tmp_path / "tower"
    if name == "absolute":
        name = str(tmp_path / name)

    with pytest.raises(ValidationError):
        write_snapshot(world, {"day": 0}, name)

    assert not (world / "snapshots").exists()
    assert not (world / "evil.json").exists()
    assert not (tmp_path / "absolute.json").exists()


def test_write_snapshot_preserves_other_double_dot_names(tmp_path: Path) -> None:
    world = tmp_path / "tower"

    snapshot = write_snapshot(world, {"day": 0}, "report..final")

    assert snapshot == world / "snapshots" / "report..final.json"
    assert load_snapshot(snapshot) == {"day": 0, "edge_key_encoding": "json-pair-v1"}


def test_diff_states_reports_left_and_right_for_changed_keys() -> None:
    left = {"morale": 50, "reputation": 50, "day": 3}
    right = {"morale": 45, "reputation": 50, "day": 3}

    assert diff_states(left, right) == {"morale": {"left": 50, "right": 45}}


def test_diff_states_reports_one_sided_keys_with_none_fallback() -> None:
    left = {"day": 3, "only_left": "scar"}
    right = {"day": 3, "only_right": "new-hire"}

    assert diff_states(left, right) == {
        "only_left": {"left": "scar", "right": None},
        "only_right": {"left": None, "right": "new-hire"},
    }


def test_diff_states_omits_equal_keys_while_reporting_changed_ones() -> None:
    left = {"morale": 50, "reputation": 50, "day": 3}
    right = {"morale": 50, "reputation": 40, "day": 3}

    assert diff_states(left, right) == {"reputation": {"left": 50, "right": 40}}


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


@pytest.mark.parametrize("delta", ["5", None, [5], True, False])
def test_dial_delta_rejects_non_numeric_delta_types(delta: object) -> None:
    state = {"day": 0, "budget": 10}
    event = {"type": "dial_delta", "day": 1, "dials": {"budget": delta}}

    with pytest.raises(ValidationError, match="budget.*int or float"):
        apply_event(state, event)


def test_dial_delta_applies_integer_delta() -> None:
    state = {"day": 0, "budget": 10}
    event = {"type": "dial_delta", "day": 1, "dials": {"budget": 3}}

    assert apply_event(state, event)["budget"] == 13


def test_dial_delta_applies_float_delta() -> None:
    state = {"day": 0, "budget": 10}
    event = {"type": "dial_delta", "day": 1, "dials": {"budget": 1.5}}

    assert apply_event(state, event)["budget"] == 11.5


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
    assert qualities["trust"]["history"][1] == {
        "event_id": "evt-looser",
        "delta": 8,
        "cap": -5,
        "floor": None,
    }


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
    assert qualities["fear"]["history"][1] == {
        "event_id": "evt-looser",
        "delta": -8,
        "cap": None,
        "floor": 5,
    }


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


@pytest.mark.parametrize(
    ("quality", "bound_name", "bound_value", "later_delta", "expected_cap", "expected_floor"),
    [
        pytest.param("trust", "cap", -5, 8, -5, None, id="inherited-cap"),
        pytest.param("fear", "floor", 5, -8, None, 5, id="inherited-floor"),
    ],
)
def test_delta_only_history_keeps_inherited_bounds_and_prior_provenance(
    quality: str,
    bound_name: str,
    bound_value: int,
    later_delta: int,
    expected_cap: int | None,
    expected_floor: int | None,
) -> None:
    scar = {
        "type": "edge_delta",
        "day": 1,
        "event_id": "evt-bound",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {quality: {"delta": bound_value, bound_name: bound_value}},
    }
    delta_only = {
        "type": "edge_delta",
        "day": 2,
        "event_id": "evt-delta",
        "from": "jordan-vale",
        "to": "sam-oduya",
        "edges": {quality: {"delta": later_delta}},
    }
    scarred = apply_event({"day": 0}, scar)
    first_row = {
        "event_id": "evt-bound",
        "delta": bound_value,
        "cap": expected_cap,
        "floor": expected_floor,
    }
    assert edge_qualities(scarred, "jordan-vale", "sam-oduya")[quality]["history"] == [first_row]
    assert edge_provenance(scarred, "jordan-vale", "sam-oduya", quality) == ["evt-bound"]

    updated = apply_event(scarred, delta_only)

    entry = edge_qualities(updated, "jordan-vale", "sam-oduya")[quality]
    assert entry["value"] == bound_value
    assert entry["cap"] == expected_cap
    assert entry["floor"] == expected_floor
    assert entry["history"] == [
        first_row,
        {
            "event_id": "evt-delta",
            "delta": later_delta,
            "cap": expected_cap,
            "floor": expected_floor,
        },
    ]
    assert edge_qualities(scarred, "jordan-vale", "sam-oduya")[quality]["history"] == [first_row]
    assert edge_provenance(updated, "jordan-vale", "sam-oduya", quality) == [
        "evt-bound",
        "evt-delta",
    ]


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


@pytest.mark.parametrize(
    "event",
    [
        {"type": "incident", "day": 1},
        {"type": "incident", "day": 1, "incident": []},
        {"type": "incident", "day": 1, "incident": {"morale_delta": "-2"}},
        {"type": "incident", "day": 1, "incident": {"morale_delta": True}},
    ],
)
def test_apply_event_rejects_malformed_incident_payload(event: dict) -> None:
    with pytest.raises(ValidationError):
        apply_event({"day": 0, "morale": 50}, event)


def test_replay_events_rejects_malformed_incident_payload(tmp_path: Path) -> None:
    event = {"type": "incident", "day": 1, "incident": []}
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        replay_events({"day": 0, "morale": 50}, path)


@pytest.mark.parametrize(("morale", "delta", "expected"), [(10, -25, -15), (140, 25, 165)])
def test_replay_keeps_out_of_range_legacy_morale(
    tmp_path: Path, morale: int, delta: int, expected: int
) -> None:
    path = tmp_path / "events.jsonl"
    event = {"type": "incident", "day": 1, "incident": {"morale_delta": delta}}
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    replayed = replay_events({"day": 0, "morale": morale}, path)

    assert replayed["morale"] == expected


def test_replay_mixes_legacy_and_versioned_dial_receipts(tmp_path: Path) -> None:
    legacy = {"type": "incident", "day": 1, "incident": {"morale_delta": -80}}
    versioned = resolve_dial_movement(
        {"type": "incident", "day": 2, "incident": {"morale_delta": -5}},
        load_rulebook(tmp_path),
    )
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(legacy) + "\n" + json.dumps(versioned) + "\n", encoding="utf-8"
    )

    replayed = replay_events({"day": 0, "morale": 50}, path)

    assert replayed["morale"] == 0


@pytest.mark.parametrize(
    ("incident", "expected_morale"),
    [({}, 50), ({"morale_delta": -2}, 48), ({"morale_delta": -2.5}, 47.5)],
)
def test_apply_event_keeps_valid_incident_morale_delta_behavior(
    incident: dict, expected_morale: float
) -> None:
    event = {"type": "incident", "day": 1, "incident": incident}

    result = apply_event({"day": 0, "morale": 50}, event)

    assert result["day"] == 1
    assert result["morale"] == expected_morale


def _edge_event(event_id: str, from_id: str, to_id: str, delta: int = 1) -> dict:
    return {
        "type": "edge_delta",
        "day": 1,
        "event_id": event_id,
        "from": from_id,
        "to": to_id,
        "edges": {"trust": {"delta": delta}},
    }


def test_json_pair_edge_encoding_keeps_delimiter_pairs_and_arbitrary_ids_distinct() -> None:
    state = {"day": 0, "edge_key_encoding": "json-pair-v1"}
    first = ("a->b", 'c"雪')
    second = ("a", 'b->c"雪')

    state = apply_event(state, _edge_event("first", *first))
    state = apply_event(state, _edge_event("second", *second, delta=2))

    expected_keys = {
        json.dumps(list(first), separators=(",", ":")),
        json.dumps(list(second), separators=(",", ":")),
    }
    assert set(state["edges"]) == expected_keys
    assert edge_qualities(state, *first)["trust"]["value"] == 1
    assert edge_qualities(state, *second)["trust"]["value"] == 2
    assert edge_provenance(state, *first, "trust") == ["first"]
    assert edge_provenance(state, *second, "trust") == ["second"]
    assert {(edge["from"], edge["to"]) for edge in character_edges(state, "a")} == {second}
    assert {(edge["from"], edge["to"]) for edge in character_edges(state, "a->b")} == {first}
    visible = {(edge["from"], edge["to"]) for edge in edges_at_or_above(state, "trust", 1)}
    assert visible == {first, second}


def test_markerless_state_reads_as_legacy_and_reducer_migrates_then_adds_distinct_pair() -> None:
    legacy = {
        "day": 0,
        "edges": {
            "a->b->c": {
                "trust": {
                    "value": 4,
                    "cap": None,
                    "floor": None,
                    "history": [{"event_id": "legacy", "delta": 4, "cap": None, "floor": None}],
                }
            }
        },
    }
    assert edge_qualities(legacy, "a", "b->c")["trust"]["value"] == 4
    assert edge_qualities(legacy, "a->b", "c")["trust"]["value"] == 4

    migrated = apply_event(legacy, _edge_event("new", "a->b", "c", delta=2))

    assert "edge_key_encoding" not in legacy
    assert edge_qualities(legacy, "a", "b->c")["trust"]["value"] == 4
    assert migrated["edge_key_encoding"] == "json-pair-v1"
    assert edge_qualities(migrated, "a", "b->c")["trust"]["value"] == 4
    assert edge_qualities(migrated, "a->b", "c")["trust"]["value"] == 2
    assert edge_provenance(migrated, "a", "b->c", "trust") == ["legacy"]
    assert edge_provenance(migrated, "a->b", "c", "trust") == ["new"]


def test_empty_replay_normalizes_legacy_copy_without_mutating_input(tmp_path: Path) -> None:
    initial = {"day": 0, "edges": {"x->y": {}}}
    events = tmp_path / "events.jsonl"
    events.write_text("", encoding="utf-8")

    replayed = replay_events(initial, events)

    assert replayed["edge_key_encoding"] == "json-pair-v1"
    assert replayed["edges"] == {'["x","y"]': {}}
    assert "edge_key_encoding" not in initial


def test_replay_preserves_both_legacy_collision_event_pairs(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(_edge_event("first", "a->b", "c"))
        + "\n"
        + json.dumps(_edge_event("second", "a", "b->c", delta=2))
        + "\n",
        encoding="utf-8",
    )

    replayed = replay_events({"day": 0}, events)

    assert edge_qualities(replayed, "a->b", "c")["trust"]["value"] == 1
    assert edge_qualities(replayed, "a", "b->c")["trust"]["value"] == 2


def test_unknown_explicit_edge_encoding_rejected_by_state_access_and_reducer() -> None:
    state = {"day": 0, "edges": {}, "edge_key_encoding": "future-v9"}
    for read in (
        lambda: edge_qualities(state, "a", "b"),
        lambda: edge_provenance(state, "a", "b", "trust"),
        lambda: character_edges(state, "a"),
        lambda: edges_at_or_above(state, "trust", 1),
        lambda: apply_event(state, {"type": "quiet_day", "day": 1}),
    ):
        with pytest.raises(ValidationError, match="edge_key_encoding: unsupported encoding"):
            read()

    null_state = {"day": 0, "edges": {}, "edge_key_encoding": None}
    for read in (
        lambda: edge_qualities(null_state, "a", "b"),
        lambda: edge_provenance(null_state, "a", "b", "trust"),
        lambda: character_edges(null_state, "a"),
        lambda: edges_at_or_above(null_state, "trust", 1),
        lambda: apply_event(null_state, {"type": "quiet_day", "day": 1}),
    ):
        with pytest.raises(ValidationError, match="edge_key_encoding: unsupported encoding"):
            read()


def test_load_world_rejects_unknown_edge_encoding(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    path = world / "state" / "tower.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["edge_key_encoding"] = "future-v9"
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValidationError, match="edge_key_encoding: unsupported encoding"):
        load_world(world)

    state["edge_key_encoding"] = None
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValidationError, match="edge_key_encoding: unsupported encoding"):
        load_world(world)


def test_new_tower_save_reload_selects_json_pair_v1(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)

    loaded = load_world(world)

    assert loaded.state["edge_key_encoding"] == "json-pair-v1"


def test_load_world_legacy_edges_are_read_without_rewriting_source_bytes(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    path = world / "state" / "tower.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state.pop("edge_key_encoding")
    state["edges"] = {"a->b->c": {"trust": {"value": 3, "history": []}}}
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    before = path.read_bytes()

    loaded = load_world(world)
    assert edge_qualities(loaded.state, "a", "b->c")["trust"]["value"] == 3
    assert path.read_bytes() == before


def test_load_snapshot_preserves_legacy_bytes_and_rejects_unknown_marker(tmp_path: Path) -> None:
    path = tmp_path / "legacy.json"
    path.write_text('{"edges":{"a->b->c":{}},"day":0}\n', encoding="utf-8")
    before = path.read_bytes()
    loaded = load_snapshot(path)
    assert "edge_key_encoding" not in loaded
    assert character_edges(loaded, "a") == [{"from": "a", "to": "b->c", "qualities": {}}]
    assert path.read_bytes() == before

    path.write_text('{"edge_key_encoding":"future-v9","day":0}\n', encoding="utf-8")
    with pytest.raises(ValidationError, match="edge_key_encoding: unsupported encoding"):
        load_snapshot(path)

    path.write_text('{"edge_key_encoding":null,"day":0}\n', encoding="utf-8")
    with pytest.raises(ValidationError, match="edge_key_encoding: unsupported encoding"):
        load_snapshot(path)


def test_write_snapshot_migrates_legacy_copy_without_mutating_input(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    legacy = {"day": 0, "edges": {"a->b->c": {"trust": {"value": 2, "history": []}}}}
    path = write_snapshot(world, legacy, "legacy")

    assert "edge_key_encoding" not in legacy
    loaded = load_snapshot(path)
    assert loaded["edge_key_encoding"] == "json-pair-v1"
    assert edge_qualities(loaded, "a", "b->c")["trust"]["value"] == 2


def test_write_snapshot_normalizes_copy_and_round_trips_v1_edges(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    state = apply_event({"day": 0}, _edge_event("e1", 'quote"雪', "x->y"))
    path = write_snapshot(world, state, "day-0001")

    loaded = load_snapshot(path)
    assert loaded["edge_key_encoding"] == "json-pair-v1"
    assert edge_provenance(loaded, 'quote"雪', "x->y", "trust") == ["e1"]
    assert state["edge_key_encoding"] == "json-pair-v1"


def test_noncanonical_unicode_escape_spelling_is_rejected_consistently(tmp_path: Path) -> None:
    escaped = json.dumps(["a", "雪"], separators=(",", ":"))
    literal = '["a","雪"]'
    canonical_state = {
        "edge_key_encoding": "json-pair-v1",
        "edges": {
            escaped: {
                "trust": {"value": 1, "history": [{"event_id": "canonical"}]}
            }
        },
    }
    assert escaped != literal
    assert edge_qualities(canonical_state, "a", "雪")["trust"]["value"] == 1
    assert edge_provenance(canonical_state, "a", "雪", "trust") == ["canonical"]
    assert character_edges(canonical_state, "a")[0]["to"] == "雪"
    assert edges_at_or_above(canonical_state, "trust", 1) == [
        {"from": "a", "to": "雪", "value": 1}
    ]

    noncanonical_state = {
        "edge_key_encoding": "json-pair-v1",
        "edges": {literal: {"trust": {"value": 1}}},
    }
    for read in (
        lambda: edge_qualities(noncanonical_state, "a", "雪"),
        lambda: edge_provenance(noncanonical_state, "a", "雪", "trust"),
        lambda: character_edges(noncanonical_state, "a"),
        lambda: edges_at_or_above(noncanonical_state, "trust", 1),
    ):
        with pytest.raises(ValidationError, match="canonical string pair"):
            read()

    path = tmp_path / "noncanonical.json"
    path.write_text(json.dumps(noncanonical_state), encoding="utf-8")
    with pytest.raises(ValidationError, match="canonical string pair"):
        load_snapshot(path)

    world = tmp_path / "tower"
    init_world(world, seed=42)
    tower_path = world / "state" / "tower.json"
    tower = json.loads(tower_path.read_text(encoding="utf-8"))
    tower["edges"] = noncanonical_state["edges"]
    tower_path.write_text(json.dumps(tower), encoding="utf-8")
    with pytest.raises(ValidationError, match="canonical string pair"):
        load_world(world)


def _offer_state_for_expiry_test() -> tuple[dict, dict, dict]:
    initial = {
        "day": 1,
        "budget": 0,
        "morale": 50,
        "reputation": 50,
        "rooms": [],
        "characters": [],
    }
    offer = {
        "type": "contract_offer",
        "day": 1,
        "offer_id": "offer-expiry-boundary",
        "created_day": 1,
        "expires_day": 4,
        "client": "Client",
        "terms": {
            "required_work_units": 2,
            "duration_ticks": 3,
            "required_room_kind": "work",
            "payout_budget": 40,
            "miss_penalty_budget": 20,
            "miss_penalty_reputation": 5,
            "pressure_milestones": [],
        },
    }
    return initial, offer, apply_event(initial, offer)


def _contract_transition_event(offer: dict, event_type: str, day: int) -> dict:
    event = {
        "type": event_type,
        "day": day,
        "contract_id": offer["offer_id"],
    }
    if event_type == "contract_expired":
        event.pop("contract_id")
        event["offer_id"] = offer["offer_id"]
    elif event_type == "contract_accepted":
        event.update(
            team_ids=["worker"],
            work_room_id="room",
            terms=offer["terms"],
        )
    return event


@pytest.mark.parametrize(
    ("event_type", "event_day"),
    [
        ("contract_accepted", 4),
        ("contract_accepted", 6),
        ("contract_declined", 4),
        ("contract_declined", 6),
        ("contract_expired", 3),
    ],
)
def test_contract_offer_transitions_enforce_frozen_expiry_day_in_reducer(
    event_type: str, event_day: int
) -> None:
    initial, offer, offered = _offer_state_for_expiry_test()
    initial_before = json.loads(json.dumps(initial))
    offered_before = json.loads(json.dumps(offered))
    event = _contract_transition_event(offer, event_type, event_day)
    event_before = json.loads(json.dumps(event))
    message = (
        r"has not expired until day 4"
        if event_type == "contract_expired"
        else r"expired on day 4"
    )

    with pytest.raises(ValidationError, match=message):
        apply_event(offered, event)

    assert initial == initial_before
    assert offered == offered_before
    assert event == event_before


@pytest.mark.parametrize(
    ("event_type", "event_day"),
    [
        ("contract_accepted", 4),
        ("contract_accepted", 6),
        ("contract_declined", 4),
        ("contract_declined", 6),
        ("contract_expired", 3),
    ],
)
def test_replay_enforces_frozen_contract_offer_expiry_day(
    tmp_path: Path, event_type: str, event_day: int
) -> None:
    initial, offer, _offered = _offer_state_for_expiry_test()
    initial_before = json.loads(json.dumps(initial))
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                offer,
                _contract_transition_event(offer, event_type, event_day),
            )
        ),
        encoding="utf-8",
    )
    message = (
        r"has not expired until day 4"
        if event_type == "contract_expired"
        else r"expired on day 4"
    )

    with pytest.raises(ValidationError, match=message):
        replay_events(initial, events_path)

    assert initial == initial_before


@pytest.mark.parametrize(
    ("event_type", "event_day", "expected_status"),
    [
        ("contract_accepted", 3, "accepted"),
        ("contract_declined", 3, "declined"),
        ("contract_expired", 4, "expired"),
        ("contract_expired", 6, "expired"),
    ],
)
def test_contract_offer_transitions_accept_valid_frozen_expiry_boundaries(
    event_type: str, event_day: int, expected_status: str
) -> None:
    _initial, offer, offered = _offer_state_for_expiry_test()

    transitioned = apply_event(
        offered, _contract_transition_event(offer, event_type, event_day)
    )

    assert transitioned["contracts"][offer["offer_id"]]["status"] == expected_status


@pytest.mark.parametrize(
    ("event_type", "event_day", "expected_status"),
    [
        ("contract_accepted", 3, "accepted"),
        ("contract_declined", 3, "declined"),
        ("contract_expired", 4, "expired"),
        ("contract_expired", 6, "expired"),
    ],
)
def test_replay_accepts_valid_frozen_contract_offer_expiry_boundaries(
    tmp_path: Path, event_type: str, event_day: int, expected_status: str
) -> None:
    initial, offer, _offered = _offer_state_for_expiry_test()
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                offer,
                _contract_transition_event(offer, event_type, event_day),
            )
        ),
        encoding="utf-8",
    )

    replayed = replay_events(initial, events_path)

    assert replayed["contracts"][offer["offer_id"]]["status"] == expected_status
