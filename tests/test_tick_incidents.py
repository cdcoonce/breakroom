import json
from pathlib import Path

import pytest

from breakroom.cli import main
from breakroom.resolution.incidents import load_incident_table
from breakroom.worldstate import ValidationError


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def events_of(world: Path, event_type: str) -> list[dict]:
    return [event for event in read_jsonl(world / "events.jsonl") if event["type"] == event_type]


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
    spotlight_records = [record for record in rolls if record["stream"] == "spotlight"]
    assert len(spotlight_records) == 1
    assert spotlight_records[0]["primitive"] == "weighted_choice"
    assert spotlight_records[0]["result"] == scene_event["incident_id"]


def test_same_seed_produces_the_same_incident_events_across_two_worlds(
    tmp_path: Path, stub_narrator
) -> None:
    worlds = [tmp_path / "tower-a", tmp_path / "tower-b"]
    for world in worlds:
        assert main(["init", "--world", str(world), "--seed", "7"]) == 0
        assert main(["tick", "--world", str(world)]) == 0

    incident_events = [events_of(world, "incident") for world in worlds]
    assert incident_events[0] == incident_events[1]
