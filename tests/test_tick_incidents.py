import json
import re
from pathlib import Path

import pytest

from breakroom.cli import main
from breakroom.resolution.incidents import load_incident_table
from breakroom.tick import QUIET_DAY_PROSE, TickError
from breakroom.worldstate import ValidationError

# Mirrors STARTER_INCIDENTS in breakroom.init: which room each starter incident points
# at, so the missing-room test can confirm the raised error names the right pair
# without hardcoding which incident the seeded spotlight draw happens to pick.
STARTER_INCIDENT_ROOMS = {
    "coffee-spill": "break-room",
    "printer-jam": "open-office",
    "awkward-silence": "break-room",
}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def events_of(world: Path, event_type: str) -> list[dict]:
    return [event for event in read_jsonl(world / "events.jsonl") if event["type"] == event_type]


def silence_incidents(world: Path) -> None:
    """Force a zero-incident tick by zeroing every base_rate.

    `bernoulli(probability=0.0)` never fires, so this makes the quiet day
    deterministic rather than waiting on an unlucky seed. Rewriting the rate
    rather than deleting the table keeps the roll receipts: each incident is
    still rolled for and still lands in the roll log.
    """
    table = world / "data" / "incidents.toml"
    table.write_text(re.sub(r"base_rate = [\d.]+", "base_rate = 0.0", table.read_text()))


@pytest.fixture
def stub_narrator(monkeypatch) -> None:
    def render_scene(brief: dict) -> str:
        return f"{brief['character']['name']} faced {brief['incident']['name']}."

    monkeypatch.setattr("breakroom.tick.render_scene", render_scene)


def test_init_scaffolds_an_incident_table_that_load_incident_table_accepts(
    tmp_path: Path,
) -> None:
    world = tmp_path / "tower"

    assert main(["init", "--world", str(world), "--seed", "42"]) == 0

    assert (world / "data" / "incidents.toml").exists()
    table = load_incident_table(world)
    assert set(table.incidents) == {"coffee-spill", "printer-jam", "awkward-silence"}


def test_tick_without_an_incident_table_is_fatal(tmp_path: Path, stub_narrator) -> None:
    world = tmp_path / "tower"
    assert main(["init", "--world", str(world), "--seed", "42"]) == 0
    (world / "data" / "incidents.toml").unlink()

    with pytest.raises(ValidationError, match="missing file"):
        main(["tick", "--world", str(world)])


def test_tick_raises_a_descriptive_error_when_the_spotlight_room_is_missing(
    tmp_path: Path, stub_narrator
) -> None:
    world = tmp_path / "tower"
    assert main(["init", "--world", str(world), "--seed", "42"]) == 0

    tower_path = world / "state" / "tower.json"
    tower_state = json.loads(tower_path.read_text())
    tower_state["rooms"] = []
    tower_path.write_text(json.dumps(tower_state))

    with pytest.raises(TickError) as exc_info:
        main(["tick", "--world", str(world)])

    message = str(exc_info.value)
    matched_incident_ids = [
        incident_id for incident_id in STARTER_INCIDENT_ROOMS if incident_id in message
    ]
    assert len(matched_incident_ids) == 1
    assert STARTER_INCIDENT_ROOMS[matched_incident_ids[0]] in message


def test_tick_emits_one_incident_event_per_fired_incident_and_one_scene(
    tmp_path: Path, stub_narrator
) -> None:
    world = tmp_path / "tower"
    assert main(["init", "--world", str(world), "--seed", "42"]) == 0

    assert main(["tick", "--world", str(world)]) == 0

    # base_rate = 1.0 on all three starter incidents, so every tick fires all of them.
    incident_events = events_of(world, "incident")
    assert {event["incident"]["id"] for event in incident_events} == {
        "coffee-spill",
        "printer-jam",
        "awkward-silence",
    }
    assert len(events_of(world, "scene")) == 1


def test_a_tick_where_no_incident_fires_is_a_quiet_day_not_a_crash(
    tmp_path: Path, stub_narrator
) -> None:
    world = tmp_path / "tower"
    assert main(["init", "--world", str(world), "--seed", "42"]) == 0
    silence_incidents(world)

    assert main(["tick", "--world", str(world)]) == 0

    assert events_of(world, "incident") == []
    assert events_of(world, "scene") == []


def test_a_quiet_day_still_advances_the_day_and_writes_a_scene_free_chronicle(
    tmp_path: Path, stub_narrator
) -> None:
    world = tmp_path / "tower"
    assert main(["init", "--world", str(world), "--seed", "42"]) == 0
    silence_incidents(world)

    assert main(["tick", "--world", str(world)]) == 0

    assert json.loads((world / "state" / "tower.json").read_text())["day"] == 1
    chronicle = (world / "chronicles" / "day-0001.md").read_text()
    assert chronicle.startswith("# Day 0001")
    assert QUIET_DAY_PROSE in chronicle
    assert "None" not in chronicle.split("## Trace")[0]


def test_a_quiet_day_records_the_rolls_that_made_it_quiet(tmp_path: Path, stub_narrator) -> None:
    world = tmp_path / "tower"
    assert main(["init", "--world", str(world), "--seed", "42"]) == 0
    silence_incidents(world)

    assert main(["tick", "--world", str(world)]) == 0

    # Without a scene event there is nothing else carrying the roll log, so a quiet
    # day would otherwise leave no trace at all of why nothing happened.
    quiet_events = events_of(world, "quiet_day")
    assert len(quiet_events) == 1
    assert quiet_events[0]["day"] == 1
    rolls = quiet_events[0]["rolls"]
    assert rolls
    assert {record["stream"] for record in rolls} == {"incidents"}
    assert all(record["result"] is False for record in rolls)


def test_a_quiet_day_never_calls_the_narrator(tmp_path: Path, monkeypatch) -> None:
    def render_scene(brief: dict) -> str:
        raise AssertionError("a quiet day has no scene to narrate")

    monkeypatch.setattr("breakroom.tick.render_scene", render_scene)
    world = tmp_path / "tower"
    assert main(["init", "--world", str(world), "--seed", "42"]) == 0
    silence_incidents(world)

    assert main(["tick", "--world", str(world)]) == 0


def test_storylet_min_tick_gap_persists_across_real_ticks(tmp_path: Path, stub_narrator) -> None:
    from breakroom.tick import tick_world
    from breakroom.worldstate import load_world

    world = tmp_path / "tower"
    assert main(["init", "--world", str(world), "--seed", "42"]) == 0

    state_path = world / "state" / "tower.json"
    older_state = json.loads(state_path.read_text())
    older_state.pop("storylet_history", None)
    state_path.write_text(json.dumps(older_state), encoding="utf-8")
    assert "storylet_history" not in load_world(world).state

    # A pre-upgrade event log is not a source for backfilling this optional map.
    old_scene = {"type": "scene", "day": 0, "storylet_id": "shared-space-repair"}
    (world / "events.jsonl").write_text(json.dumps(old_scene) + "\n", encoding="utf-8")

    storylet_dir = world / "data" / "storylets"
    for storylet_path in storylet_dir.glob("*.toml"):
        storylet_path.unlink()
    (storylet_dir / "shared-space-repair.toml").write_text(
        '''
id = "shared-space-repair"
title = "Shared Space Repair"
premise = "A small mess tests shared responsibility."
kind = "incident_response"

[eligibility]
incident_ids = ["coffee-spill"]
min_tick_gap = 3

[[participants]]
slot = "cleanup_owner"
source = "incident.cleanup_owner"
required = true

[[decision_points]]
id = "shared-space-repair-response"
decision_type = "incident_response"
character_slot = "cleanup_owner"
''',
        encoding="utf-8",
    )

    expected_history = {"shared-space-repair": 1}
    for day in range(1, 5):
        # Each production tick reloads the persisted tower state.
        assert load_world(world).state["day"] == day - 1
        tick_world(world)
        state = json.loads(state_path.read_text())
        if day < 4:
            assert state["storylet_history"] == expected_history
        else:
            assert state["storylet_history"] == {"shared-space-repair": 4}

    assert [
        event["day"] for event in events_of(world, "scene") if event["day"] > 0
    ] == [1, 4]


def test_morale_reflects_the_sum_of_every_fired_incidents_delta(
    tmp_path: Path, stub_narrator
) -> None:
    world = tmp_path / "tower"
    assert main(["init", "--world", str(world), "--seed", "42"]) == 0
    starting_morale = json.loads((world / "state" / "tower.json").read_text())["morale"]

    assert main(["tick", "--world", str(world)]) == 0

    incident_events = events_of(world, "incident")
    expected_delta = sum(event["incident"]["morale_delta"] for event in incident_events)
    state = json.loads((world / "state" / "tower.json").read_text())
    assert state["morale"] == starting_morale + expected_delta


def test_scene_event_carries_a_roll_log_with_incidents_and_spotlight_records(
    tmp_path: Path, stub_narrator
) -> None:
    world = tmp_path / "tower"
    assert main(["init", "--world", str(world), "--seed", "42"]) == 0

    assert main(["tick", "--world", str(world)]) == 0

    scene_event = events_of(world, "scene")[0]
    rolls = scene_event["rolls"]
    assert rolls
    for record in rolls:
        assert set(record) == {"stream", "tick", "purpose", "primitive", "result"}
    assert any(record["stream"] == "incidents" for record in rolls)


def test_spotlight_is_drawn_from_a_dedicated_stream_not_the_incidents_stream(
    tmp_path: Path, stub_narrator
) -> None:
    world = tmp_path / "tower"
    assert main(["init", "--world", str(world), "--seed", "42"]) == 0

    assert main(["tick", "--world", str(world)]) == 0

    scene_event = events_of(world, "scene")[0]
    rolls = scene_event["rolls"]
    spotlight_records = [record for record in rolls if record["stream"] == "storylet_select"]
    assert len(spotlight_records) == 1
    assert spotlight_records[0]["primitive"] == "weighted_choice"
    assert spotlight_records[0]["result"] == scene_event["storylet_id"]


def test_same_seed_produces_the_same_incident_events_across_two_worlds(
    tmp_path: Path, stub_narrator
) -> None:
    worlds = [tmp_path / "tower-a", tmp_path / "tower-b"]
    for world in worlds:
        assert main(["init", "--world", str(world), "--seed", "7"]) == 0
        assert main(["tick", "--world", str(world)]) == 0

    incident_events = [events_of(world, "incident") for world in worlds]
    assert incident_events[0] == incident_events[1]
