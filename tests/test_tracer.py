import json
from pathlib import Path

from breakroom.cli import main


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_init_writes_starter_tower_and_character(tmp_path: Path) -> None:
    world = tmp_path / "tower"

    assert main(["init", "--world", str(world), "--seed", "42"]) == 0

    state = json.loads((world / "state" / "tower.json").read_text())
    assert state["seed"] == 42
    assert state["day"] == 0
    assert state["rooms"] == [
        {"id": "break-room", "name": "Break Room", "kind": "social", "floor": 1},
        {"id": "open-office", "name": "Open Office", "kind": "work", "floor": 1},
    ]
    assert "declared_values" in (world / "characters" / "jordan-vale.toml").read_text()
    assert (world / "events.jsonl").read_text() == ""


def test_init_uses_default_world_and_seed_when_flags_omitted(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["init"]) == 0

    state = json.loads((tmp_path / "state" / "tower.json").read_text())
    assert state["seed"] == 1


def test_tick_appends_events_updates_state_and_writes_chronicle(
    tmp_path: Path, monkeypatch
) -> None:
    world = tmp_path / "tower"
    main(["init", "--world", str(world), "--seed", "42"])

    def render_scene(brief: dict) -> str:
        return f"{brief['character']['name']} faced {brief['incident']['name']}."

    monkeypatch.setattr("breakroom.tick.render_scene", render_scene)

    assert main(["tick", "--world", str(world)]) == 0
    assert main(["tick", "--world", str(world)]) == 0

    state = json.loads((world / "state" / "tower.json").read_text())
    assert state["day"] == 2
    assert state["morale"] == 40
    assert state["reputation"] == 50

    events = read_jsonl(world / "events.jsonl")
    # base_rate = 1.0 on every starter incident fires all three each tick: one "incident"
    # event apiece, plus one "scene" event for the spotlight draw.
    assert [event["type"] for event in events] == [
        "incident",
        "incident",
        "incident",
        "scene",
        "incident",
        "incident",
        "incident",
        "scene",
    ]
    assert events[0]["incident"]["id"] == "awkward-silence"
    assert events[0]["storylet"]["id"] == "quiet-room"
    assert events[1]["incident"]["id"] == "coffee-spill"
    assert events[2]["incident"]["id"] == "printer-jam"
    assert events[3]["incident_id"] == "awkward-silence"
    assert [event["sequence"] for event in events] == [1, 2, 3, 4, 5, 6, 7, 8]

    first_chronicle = (world / "chronicles" / "day-0001.md").read_text()
    assert "Jordan Vale faced Awkward Silence." in first_chronicle
    assert "brief:" in first_chronicle


def test_same_seed_and_starting_state_reproduce_mechanical_events(
    tmp_path: Path, monkeypatch
) -> None:
    def render_scene(brief: dict) -> str:
        return f"{brief['character']['name']} faced {brief['incident']['name']}."

    monkeypatch.setattr("breakroom.tick.render_scene", render_scene)
    worlds = [tmp_path / "tower-a", tmp_path / "tower-b"]

    for world in worlds:
        main(["init", "--world", str(world), "--seed", "42"])
        main(["tick", "--world", str(world)])
        main(["tick", "--world", str(world)])

    event_logs = [read_jsonl(world / "events.jsonl") for world in worlds]
    mechanical_events = [
        [event for event in event_log if event["type"] == "incident"] for event_log in event_logs
    ]
    assert mechanical_events[0] == mechanical_events[1]
