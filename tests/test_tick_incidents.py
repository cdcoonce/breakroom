import json
import re
import shlex
import sys
from pathlib import Path

import pytest

from breakroom import storylets, worldstate
from breakroom.cli import main
from breakroom.events import append_event
from breakroom.init import init_world
from breakroom.resolution.incidents import load_incident_table
from breakroom.tick import QUIET_DAY_PROSE, TickError, tick_world
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
    original_state_bytes = tower_path.read_bytes()
    tower_state = json.loads(original_state_bytes)
    tower_state["rooms"] = []
    tower_path.write_text(json.dumps(tower_state))
    failed_state_bytes = tower_path.read_bytes()

    unrelated_incident = append_event(
        world,
        {
            "type": "incident",
            "day": 0,
            "incident": {"id": "unrelated-prior-incident"},
        },
    )
    events_path = world / "events.jsonl"
    events_before_failure = events_path.read_bytes()

    with pytest.raises(TickError) as exc_info:
        main(["tick", "--world", str(world)])

    message = str(exc_info.value)
    matched_incident_ids = [
        incident_id for incident_id in STARTER_INCIDENT_ROOMS if incident_id in message
    ]
    assert len(matched_incident_ids) == 1
    spotlight_room = STARTER_INCIDENT_ROOMS[matched_incident_ids[0]]
    assert spotlight_room in message
    assert events_path.read_bytes() == events_before_failure
    assert tower_path.read_bytes() == failed_state_bytes

    # Repair only the missing dependency and retry the same day. The failed attempt
    # must not leave orphan incident receipts that the retry would duplicate.
    tower_path.write_bytes(original_state_bytes)
    assert main(["tick", "--world", str(world)]) == 0

    saved_state = json.loads(tower_path.read_text())
    assert saved_state["day"] == 1
    events = read_jsonl(events_path)
    assert events[0] == unrelated_incident
    day_one_events = [event for event in events if event.get("day") == 1]
    assert [event["type"] for event in day_one_events] == [
        "incident",
        "incident",
        "incident",
        "scene",
    ]
    assert sorted(
        event["incident"]["id"]
        for event in day_one_events
        if event["type"] == "incident"
    ) == sorted(STARTER_INCIDENT_ROOMS)


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


def test_failed_narration_leaves_incident_receipts_for_a_successful_retry(
    tmp_path: Path, monkeypatch
) -> None:
    world = tmp_path / "tower"
    assert main(["init", "--world", str(world), "--seed", "42"]) == 0

    events_path = world / "events.jsonl"
    tower_path = world / "state" / "tower.json"
    prior_event = append_event(
        world,
        {
            "type": "incident",
            "day": 0,
            "incident": {"id": "unrelated-prior-incident", "morale_delta": 0},
        },
    )
    events_before_failure = events_path.read_bytes()
    state_before_failure = tower_path.read_bytes()
    pre_tick_state = json.loads(state_before_failure)

    def fail_narration(_brief: dict) -> str:
        raise RuntimeError("narrator unavailable")

    monkeypatch.setattr("breakroom.tick.render_scene", fail_narration)
    with pytest.raises(RuntimeError, match="narrator unavailable"):
        tick_world(world)

    assert events_path.read_bytes() == events_before_failure
    assert tower_path.read_bytes() == state_before_failure

    monkeypatch.setattr("breakroom.tick.render_scene", lambda _brief: "A recovered scene.")
    tick_world(world)

    saved_state = json.loads(tower_path.read_text())
    assert saved_state["day"] == 1
    events = read_jsonl(events_path)
    assert events[0] == prior_event
    day_one_events = [event for event in events if event.get("day") == 1]
    assert [event["type"] for event in day_one_events] == [
        "incident",
        "incident",
        "incident",
        "scene",
    ]
    assert sorted(
        event["incident"]["id"]
        for event in day_one_events
        if event["type"] == "incident"
    ) == sorted(STARTER_INCIDENT_ROOMS)
    assert worldstate.replay_events(pre_tick_state, events_path) == saved_state


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


def test_fired_incident_without_eligible_storylet_gets_factual_chronicle(
    tmp_path: Path, monkeypatch
) -> None:
    world = tmp_path / "tower"
    assert main(["init", "--world", str(world), "--seed", "42"]) == 0
    incident_table = world / "data" / "incidents.toml"
    text, replacements = re.subn(
        r'(id = "(?:coffee-spill|printer-jam)"\nbase_rate = )1\.0',
        r"\g<1>0.0",
        incident_table.read_text(),
    )
    assert replacements == 2
    incident_table.write_text(text)

    selections = []
    real_select_storylet = storylets.select_storylet

    def observe_selection(*args, **kwargs):
        selection = real_select_storylet(*args, **kwargs)
        selections.append(selection)
        return selection

    monkeypatch.setattr(storylets, "select_storylet", observe_selection)
    narrator_calls = []

    def reject_narration(_brief):
        narrator_calls.append(True)
        raise AssertionError("fired incident without an eligible storylet has no scene")

    monkeypatch.setattr("breakroom.tick.render_scene", reject_narration)
    starting_morale = json.loads((world / "state" / "tower.json").read_text())["morale"]

    assert main(["tick", "--world", str(world)]) == 0

    assert selections == [None]
    assert narrator_calls == []
    incident_events = events_of(world, "incident")
    assert len(incident_events) == 1
    incident = incident_events[0]["incident"]
    assert incident["id"] == "awkward-silence"
    assert incident["needs_cleanup"] is False
    assert incident["cleanup_owner"] is None
    state = json.loads((world / "state" / "tower.json").read_text())
    assert state["day"] == 1
    assert state["morale"] == starting_morale + incident["morale_delta"]

    quiet_events = events_of(world, "quiet_day")
    assert len(quiet_events) == 1
    assert quiet_events[0]["day"] == 1
    rolls = quiet_events[0]["rolls"]
    assert len(rolls) == 3
    assert all(record["stream"] == "incidents" and record["tick"] == 1 for record in rolls)
    assert sorted(record["result"] for record in rolls) == [False, False, True]
    assert events_of(world, "scene") == []

    chronicle = (world / "chronicles" / "day-0001.md").read_text()
    assert "Incidents fired today." in chronicle
    assert QUIET_DAY_PROSE not in chronicle


def test_fired_incident_without_detail_raises_tick_error_naming_incident(
    tmp_path: Path,
) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    (world / "data" / "incidents.toml").write_text(
        '''
[[incidents]]
id = "dial-only"
base_rate = 1.0
effects = [{ type = "dial_delta", dials = { morale = -1 } }]
''',
        encoding="utf-8",
    )

    with pytest.raises(TickError) as exc_info:
        tick_world(world)

    assert "dial-only" in str(exc_info.value)


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


@pytest.mark.parametrize("declare_empty_ids", [False, True], ids=["omitted", "empty"])
@pytest.mark.parametrize("use_command", [False, True], ids=["builtin", "local-command"])
def test_incident_free_storylet_keeps_its_scene_and_tick_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declare_empty_ids: bool,
    use_command: bool,
) -> None:
    world = tmp_path / "ambient-tower"
    init_world(world, seed=42)
    definitions = world / "data" / "storylets"
    for definition in definitions.glob("*.toml"):
        definition.unlink()
    eligibility = "incident_ids = []\n" if declare_empty_ids else ""
    (definitions / "office-pause.toml").write_text(
        'id = "office-pause"\n'
        'title = "Office Pause"\n'
        'premise = "A shared pause gives the afternoon a different rhythm."\n'
        'kind = "ambient"\n'
        '\n[eligibility]\n'
        f'{eligibility}'
        '\n[[participants]]\n'
        'slot = "colleague"\n'
        'source = "incident.cleanup_owner"\n'
        'required = true\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("BREAKROOM_NARRATOR_COMMAND", raising=False)
    expected_prose = "Jordan Vale: A shared pause gives the afternoon a different rhythm."
    if use_command:
        # A local process verifies the real JSON transport without contacting a model.
        command = "import sys; sys.stdout.write(sys.stdin.read())"
        monkeypatch.setenv(
            "BREAKROOM_NARRATOR_COMMAND", shlex.join([sys.executable, "-c", command])
        )

    tick_world(world)

    scenes = events_of(world, "scene")
    assert len(scenes) == 1
    scene = scenes[0]
    assert scene["storylet_id"] == "office-pause"
    assert scene["character_id"] == "jordan-vale"
    assert scene["character_ids"] == ["jordan-vale"]
    assert scene["brief"]["character"]["name"] == "Jordan Vale"
    assert scene["brief"]["storylet"] == {
        "id": "office-pause",
        "title": "Office Pause",
        "premise": "A shared pause gives the afternoon a different rhythm.",
    }
    assert scene["brief"]["incident"] is None
    assert scene["brief"]["room"] is None
    if use_command:
        assert json.loads(scene["prose"]) == scene["brief"]
    else:
        assert scene["prose"] == expected_prose
    scene_json = next(
        line
        for line in (world / "events.jsonl").read_text().splitlines()
        if json.loads(line)["type"] == "scene"
    )
    assert '"incident": null' in scene_json
    assert '"room": null' in scene_json
    incidents = events_of(world, "incident")
    assert sorted(event["incident"]["id"] for event in incidents) == sorted(STARTER_INCIDENT_ROOMS)
    saved_state = json.loads((world / "state" / "tower.json").read_text())
    assert saved_state["day"] == 1
    assert saved_state["morale"] == 45
    assert saved_state["spotlight_history"] == {"jordan-vale": 1}
    selections = [roll for roll in scene["rolls"] if roll["stream"] == "storylet_select"]
    assert len(selections) == 1
    assert selections[0]["result"] == "office-pause"
    assert events_of(world, "quiet_day") == []
    chronicle = (world / "chronicles" / "day-0001.md").read_text()
    assert chronicle.startswith("# Day 0001\n")
    assert scene["prose"] in chronicle


def _write_spotlight_incidents(world: Path, rates: dict[str, float]) -> None:
    incident_table = world / "data" / "incidents.toml"
    incidents = {
        "awkward-silence": ("Awkward Silence", "break-room"),
        "coffee-spill": ("Coffee Spill", "break-room"),
        "printer-jam": ("Printer Jam", "open-office"),
    }
    incident_table.write_text(
        "".join(
            f'[[incidents]]\nid = "{incident_id}"\nbase_rate = {rates[incident_id]}\n'
            f'rooms = ["{room}"]\n\n[[incidents.effects]]\n'
            f'type = "incident_detail"\nname = "{name}"\nroom = "{room}"\n'
            'morale_delta = -1\nnorm_tags = []\nneeds_cleanup = true\n\n'
            for incident_id, (name, room) in incidents.items()
        ),
        encoding="utf-8",
    )


def _write_spotlight_storylet(world: Path, incident_ids: list[str]) -> None:
    definitions = world / "data" / "storylets"
    for definition in definitions.glob("*.toml"):
        definition.unlink()
    ids = ", ".join(json.dumps(incident_id) for incident_id in incident_ids)
    (definitions / "spotlight-probe.toml").write_text(
        'id = "spotlight-probe"\n'
        'title = "Spotlight Probe"\n'
        'premise = "A fired incident anchors the scene."\n'
        'kind = "incident_response"\n\n'
        '[eligibility]\n'
        f'incident_ids = [{ids}]\n\n'
        '[[participants]]\n'
        'slot = "responder"\n'
        'source = "incident.cleanup_owner"\n'
        'required = true\n\n'
        '[[decision_points]]\n'
        'id = "spotlight-probe-choice"\n'
        'decision_type = "incident_response"\n'
        'character_slot = "responder"\n',
        encoding="utf-8",
    )


def test_nonempty_storylet_gate_uses_a_later_fired_incident(
    tmp_path: Path, stub_narrator
) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_spotlight_incidents(
        world,
        {"awkward-silence": 0.0, "coffee-spill": 1.0, "printer-jam": 0.0},
    )
    _write_spotlight_storylet(world, ["printer-jam", "coffee-spill"])

    tick_world(world)

    scene = events_of(world, "scene")[0]
    assert scene["brief"]["incident"]["id"] == "coffee-spill"
    assert scene["brief"]["room"]["id"] == scene["brief"]["incident"]["room"]


@pytest.mark.parametrize(
    "incident_ids",
    [["printer-jam", "coffee-spill"], ["coffee-spill", "printer-jam"]],
    ids=["printer-declared-first", "coffee-declared-first"],
)
def test_nonempty_storylet_gate_picks_sorted_fired_match_not_unrelated_first(
    tmp_path: Path, stub_narrator, incident_ids: list[str]
) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_spotlight_incidents(
        world,
        {"awkward-silence": 1.0, "coffee-spill": 1.0, "printer-jam": 1.0},
    )
    _write_spotlight_storylet(world, incident_ids)

    tick_world(world)

    scene = events_of(world, "scene")[0]
    assert scene["brief"]["incident"]["id"] == "coffee-spill"
    assert scene["brief"]["room"]["id"] == "break-room"

def _write_probe_incident(world: Path, effects: str) -> None:
    (world / "data" / "incidents.toml").write_text(
        '[[incidents]]\n'
        'id = "probe-incident"\n'
        'base_rate = 1.0\n'
        f'{effects}\n',
        encoding="utf-8",
    )


def _write_probe_storylet(
    world: Path, storylet_id: str = "probe-response", incident_id: str = "probe-incident"
) -> None:
    definitions = world / "data" / "storylets"
    for path in definitions.glob("*.toml"):
        path.unlink()
    (definitions / f"{storylet_id}.toml").write_text(
        f'id = "{storylet_id}"\n'
        f'title = "{storylet_id}"\n'
        'premise = "A response to the probe incident."\n'
        'kind = "incident_response"\n'
        '\n[eligibility]\n'
        f'incident_ids = ["{incident_id}"]\n'
        '\n[[participants]]\n'
        'slot = "responder"\n'
        'source = "incident.cleanup_owner"\n'
        'required = true\n'
        '\n[[decision_points]]\n'
        'id = "probe-choice"\n'
        'decision_type = "incident_response"\n'
        'character_slot = "responder"\n',
        encoding="utf-8",
    )


def _write_competing_storylets(world: Path) -> None:
    definitions = world / "data" / "storylets"
    for path in definitions.glob("*.toml"):
        path.unlink()
    for storylet_id, bias in (("steady-response", 2.0), ("urgent-response", -2.0)):
        (definitions / f"{storylet_id}.toml").write_text(
            f'id = "{storylet_id}"\n'
            f'title = "{storylet_id}"\n'
            'premise = "A response to the probe incident."\n'
            'kind = "incident_response"\n'
            '\n[eligibility]\n'
            'incident_ids = ["probe-incident"]\n'
            '\n[[participants]]\n'
            'slot = "responder"\n'
            'source = "incident.cleanup_owner"\n'
            'required = true\n'
            '\n[[decision_points]]\n'
            f'id = "{storylet_id}-choice"\n'
            'decision_type = "incident_response"\n'
            'character_slot = "responder"\n'
            '\n[salience]\n'
            f'storylet_bias = {bias}\n',
            encoding="utf-8",
        )


def test_additive_dial_effect_changes_deterministic_storylet_selection(
    tmp_path: Path, stub_narrator
) -> None:
    from breakroom.storylets import EngineContext, load_registry, select_storylet

    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_probe_incident(
        world,
        '''[[incidents.effects]]
type = "incident_detail"
name = "Probe"
room = "break-room"
morale_delta = -1
norm_tags = []
needs_cleanup = true

[[incidents.effects]]
type = "dial_delta"
dials = { morale = 1 }
incident_id = "probe-incident"
cascade_id = "probe-cascade"
depth = 0
tick = 1''',
    )
    _write_competing_storylets(world)
    state_path = world / "state" / "tower.json"
    state = json.loads(state_path.read_text())
    state["morale"] = 25
    initial_state = dict(state)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    loaded = worldstate.load_world(world)
    incident_event = {
        "type": "incident",
        "day": 1,
        "incident": {
            "id": "probe-incident",
            "name": "Probe",
            "room": "break-room",
            "morale_delta": -1,
            "norm_tags": [],
            "needs_cleanup": True,
            "cleanup_owner": "jordan-vale",
            "resolved": False,
        },
    }
    after_detail = worldstate.apply_event(state, incident_event)
    after_effect = worldstate.apply_event(
        after_detail, {"type": "dial_delta", "day": 1, "dials": {"morale": 1}}
    )
    registry = load_registry(world)
    before_context = EngineContext(
        tick=1, state=after_detail, characters=loaded.characters, incident_events=[incident_event]
    )
    after_context = EngineContext(
        tick=1, state=after_effect, characters=loaded.characters, incident_events=[incident_event]
    )
    seed = next(
        candidate
        for candidate in range(1000)
        if select_storylet(registry, context=before_context, seed=candidate).storylet.id
        != select_storylet(registry, context=after_context, seed=candidate).storylet.id
    )
    state["seed"] = seed
    state_path.write_text(json.dumps(state), encoding="utf-8")
    expected = select_storylet(registry, context=after_context, seed=seed).storylet.id

    tick_world(world)

    saved_state = json.loads(state_path.read_text())
    scene = events_of(world, "scene")[0]
    assert scene["storylet_id"] == expected
    assert expected != select_storylet(registry, context=before_context, seed=seed).storylet.id
    assert saved_state["morale"] == initial_state["morale"] - 1 + 1
    assert [event["type"] for event in read_jsonl(world / "events.jsonl")] == [
        "incident",
        "dial_delta",
        "scene",
    ]


def test_chained_edge_effects_keep_order_day_provenance_and_replay(
    tmp_path: Path, stub_narrator
) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    (world / "data" / "incidents.toml").write_text(
        '''[[incidents]]
id = "probe-a"
base_rate = 1.0
chain_triggers = [{ target = "probe-b", mode = "direct" }]

[[incidents.effects]]
type = "incident_detail"
name = "Probe A"
room = "break-room"
morale_delta = 0
norm_tags = []
needs_cleanup = true

[[incidents.effects]]
type = "edge_delta"
from = "jordan-vale"
to = "mira-okonkwo"
edges = { trust = { delta = 5, cap = 4, floor = 1 } }
event_id = "probe-edge-1"
day = 99

[[incidents]]
id = "probe-b"
base_rate = 0.0

[[incidents.effects]]
type = "incident_detail"
name = "Probe B"
room = "break-room"
morale_delta = 0
norm_tags = []
needs_cleanup = true

[[incidents.effects]]
type = "edge_delta"
from = "jordan-vale"
to = "mira-okonkwo"
edges = { trust = { delta = 2, cap = 3, floor = 2 } }
event_id = "probe-edge-2"
day = 99
''',
        encoding="utf-8",
    )
    _write_probe_storylet(world, incident_id="probe-a")
    state_path = world / "state" / "tower.json"
    initial_state = json.loads(state_path.read_text())

    tick_world(world)

    events_path = world / "events.jsonl"
    persisted = read_jsonl(events_path)
    edge_events = events_of(world, "edge_delta")
    saved_state = json.loads(state_path.read_text())
    trust = worldstate.edge_qualities(saved_state, "jordan-vale", "mira-okonkwo")["trust"]
    assert [event["type"] for event in persisted] == [
        "incident", "incident", "edge_delta", "edge_delta", "scene"
    ]
    assert [event["incident"]["id"] for event in persisted if event["type"] == "incident"] == [
        "probe-a",
        "probe-b",
    ]
    assert [event["event_id"] for event in edge_events] == ["probe-edge-1", "probe-edge-2"]
    assert [event["day"] for event in edge_events] == [1, 1]
    assert [(event["tick"], event["incident_id"], event["depth"]) for event in edge_events] == [
        (1, "probe-a", 0),
        (1, "probe-b", 1),
    ]
    assert edge_events[0]["cascade_id"] == edge_events[1]["cascade_id"]
    assert edge_events[0]["cascade_id"]
    assert trust["value"] == 3
    assert trust["cap"] == 3
    assert trust["floor"] == 2
    assert [change["event_id"] for change in trust["history"]] == ["probe-edge-1", "probe-edge-2"]
    assert worldstate.replay_events(initial_state, events_path) == saved_state


@pytest.mark.parametrize(
    ("effect", "offending"),
    [
        ('{ type = "incident" }', "'incident'"),
        ('{ type = "scene" }', "'scene'"),
        ('{ type = "quiet_day" }', "'quiet_day'"),
        ('{ type = "unsupported" }', "'unsupported'"),
        ('{ dials = { morale = 1 } }', "None"),
        ('{ type = ["dial_delta"] }', "['dial_delta']"),
    ],
    ids=["incident", "scene", "quiet-day", "unknown", "missing", "array-type"],
)
def test_invalid_incident_effect_type_fails_before_any_tick_writes(
    tmp_path: Path, effect: str, offending: str
) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_probe_incident(
        world,
        'effects = [{ type = "incident_detail", name = "Probe", room = "break-room", '
        'morale_delta = 0, norm_tags = [], needs_cleanup = false }, ' + effect + "]",
    )
    state_path = world / "state" / "tower.json"
    events_path = world / "events.jsonl"
    state_before = state_path.read_bytes()
    events_before = events_path.read_bytes() if events_path.exists() else None

    with pytest.raises(ValidationError) as exc_info:
        tick_world(world)

    assert "probe-incident" in str(exc_info.value)
    assert offending in str(exc_info.value)
    assert state_path.read_bytes() == state_before
    assert (events_path.read_bytes() if events_path.exists() else None) == events_before
    assert not (world / "chronicles" / "day-0001.md").exists()


def test_supported_edge_effect_reducer_error_happens_before_any_tick_writes(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_probe_incident(
        world,
        '''[[incidents.effects]]
type = "incident_detail"
name = "Probe"
room = "break-room"
morale_delta = 0
norm_tags = []
needs_cleanup = false

[[incidents.effects]]
type = "edge_delta"
from = "jordan-vale"
to = "mira-okonkwo"
edges = { trust = { delta = 1 } }
incident_id = "probe-incident"''',
    )
    state_path = world / "state" / "tower.json"
    events_path = world / "events.jsonl"
    state_before = state_path.read_bytes()
    events_before = events_path.read_bytes() if events_path.exists() else None

    with pytest.raises(ValidationError, match="edge_delta event: missing event_id"):
        tick_world(world)

    assert state_path.read_bytes() == state_before
    assert (events_path.read_bytes() if events_path.exists() else None) == events_before
    assert not (world / "chronicles" / "day-0001.md").exists()


def test_fired_incident_without_storylet_persists_effect_then_quiet_day_and_replays(
    tmp_path: Path, monkeypatch
) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_probe_incident(
        world,
        '''[[incidents.effects]]
type = "incident_detail"
name = "Probe"
room = "break-room"
morale_delta = -1
norm_tags = []
needs_cleanup = false

[[incidents.effects]]
type = "dial_delta"
dials = { morale = 4 }
incident_id = "probe-incident"
cascade_id = "probe-cascade"
depth = 0
tick = 1''',
    )
    for path in (world / "data" / "storylets").glob("*.toml"):
        path.unlink()
    state_path = world / "state" / "tower.json"
    initial_state = json.loads(state_path.read_text())
    def fail_narration(_brief: dict) -> str:
        pytest.fail("no scene to narrate")

    monkeypatch.setattr("breakroom.tick.render_scene", fail_narration)

    tick_world(world)

    events_path = world / "events.jsonl"
    events = read_jsonl(events_path)
    saved_state = json.loads(state_path.read_text())
    assert [event["type"] for event in events] == ["incident", "dial_delta", "quiet_day"]
    assert events[-2]["day"] == events[-1]["day"] == 1
    assert saved_state["morale"] == initial_state["morale"] + 3
    assert worldstate.replay_events(initial_state, events_path) == saved_state


def test_narrator_retry_persists_supported_effect_exactly_once_and_replays(
    tmp_path: Path, monkeypatch
) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    _write_probe_incident(
        world,
        '''[[incidents.effects]]
type = "incident_detail"
name = "Probe"
room = "break-room"
morale_delta = -1
norm_tags = []
needs_cleanup = true

[[incidents.effects]]
type = "dial_delta"
dials = { morale = 2 }
incident_id = "probe-incident"
cascade_id = "probe-cascade"
depth = 0
tick = 1''',
    )
    _write_probe_storylet(world)
    state_path = world / "state" / "tower.json"
    events_path = world / "events.jsonl"
    initial_state_bytes = state_path.read_bytes()
    initial_state = json.loads(initial_state_bytes)
    events_before = events_path.read_bytes() if events_path.exists() else None

    def fail_narration(_brief: dict) -> str:
        raise RuntimeError("narrator unavailable")

    monkeypatch.setattr("breakroom.tick.render_scene", fail_narration)
    with pytest.raises(RuntimeError, match="narrator unavailable"):
        tick_world(world)
    assert state_path.read_bytes() == initial_state_bytes
    assert (events_path.read_bytes() if events_path.exists() else None) == events_before

    monkeypatch.setattr("breakroom.tick.render_scene", lambda _brief: "Recovered scene.")
    tick_world(world)

    events = read_jsonl(events_path)
    saved_state = json.loads(state_path.read_text())
    assert [event["type"] for event in events] == ["incident", "dial_delta", "scene"]
    assert len(events_of(world, "incident")) == 1
    assert len(events_of(world, "dial_delta")) == 1
    assert saved_state["morale"] == initial_state["morale"] + 1
    assert worldstate.replay_events(initial_state, events_path) == saved_state
