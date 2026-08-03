from pathlib import Path

import pytest

from breakroom.resolution.incidents import (
    MAX_CASCADE_DEPTH,
    IncidentResolution,
    evaluate_tick,
    load_incident_table,
)
from breakroom.worldstate import ValidationError

SIMPLE_TABLE = """\
[[incidents]]
id = "coffee-spill"
base_rate = 0.4
preconditions = [
  { path = "morale", operator = "gte", value = 0 },
]
rooms = ["break-room"]
characters = []
effects = [
  { type = "dial_delta", dials = { morale = -2 } },
]
chain_triggers = []

[[incidents]]
id = "printer-jam"
base_rate = 0.2
rooms = ["open-office"]
effects = [
  { type = "dial_delta", dials = { morale = -1 } },
]
"""

CASCADE_TABLE = """\
[[incidents]]
id = "root-incident"
base_rate = 1.0
effects = [
  { type = "dial_delta", dials = { morale = -1 } },
]
chain_triggers = [
  { target = "chained-incident", mode = "direct" },
]

[[incidents]]
id = "chained-incident"
base_rate = 0.0
effects = [
  { type = "dial_delta", dials = { reputation = -1 } },
]
chain_triggers = [
  { target = "boosted-incident", mode = "boost", amount = 1.0 },
]

[[incidents]]
id = "boosted-incident"
base_rate = 0.0
effects = [
  { type = "dial_delta", dials = { morale = -3 } },
]
"""


def write_table(world: Path, toml_text: str = SIMPLE_TABLE) -> None:
    (world / "data").mkdir(parents=True, exist_ok=True)
    (world / "data" / "incidents.toml").write_text(toml_text, encoding="utf-8")


def build_chain_table(world: Path, length: int) -> None:
    lines = []
    for index in range(length):
        incident_id = f"chain-{index}"
        base_rate = 1.0 if index == 0 else 0.0
        lines.append("[[incidents]]")
        lines.append(f'id = "{incident_id}"')
        lines.append(f"base_rate = {base_rate}")
        if index + 1 < length:
            lines.append(
                f'chain_triggers = [{{ target = "chain-{index + 1}", mode = "direct" }}]'
            )
        lines.append("")
    write_table(world, "\n".join(lines))


def test_load_incident_table_parses_worked_example(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_table(world)

    table = load_incident_table(world)

    assert set(table.incidents) == {"coffee-spill", "printer-jam"}
    assert table.incidents["coffee-spill"].base_rate == 0.4
    assert table.incidents["coffee-spill"].preconditions[0].path == "morale"
    assert table.incidents["coffee-spill"].effects == [
        {"type": "dial_delta", "dials": {"morale": -2}}
    ]


def test_load_incident_table_requires_file(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    world.mkdir()

    with pytest.raises(ValidationError, match="data/incidents.toml: missing file"):
        load_incident_table(world)


def test_load_incident_table_rejects_missing_required_field(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_table(world, '[[incidents]]\nid = "coffee-spill"\n')

    with pytest.raises(ValidationError, match="missing base_rate"):
        load_incident_table(world)


def test_load_incident_table_rejects_unknown_incident_field(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_table(
        world,
        '[[incidents]]\nid = "coffee-spill"\nbase_rate = 0.4\nbogus = "nope"\n',
    )

    with pytest.raises(ValidationError, match="unknown incident fields"):
        load_incident_table(world)


def test_load_incident_table_rejects_unknown_precondition_field(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_table(
        world,
        '[[incidents]]\nid = "coffee-spill"\nbase_rate = 0.4\n'
        'preconditions = [{ path = "morale", operator = "gte", value = 0, extra = 1 }]\n',
    )

    with pytest.raises(ValidationError, match="unknown precondition fields"):
        load_incident_table(world)


def test_load_incident_table_rejects_invalid_operator(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_table(
        world,
        '[[incidents]]\nid = "coffee-spill"\nbase_rate = 0.4\n'
        'preconditions = [{ path = "morale", operator = "ne", value = 0 }]\n',
    )

    with pytest.raises(ValidationError, match="invalid precondition operator"):
        load_incident_table(world)


def test_load_incident_table_rejects_duplicate_ids(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_table(
        world,
        '[[incidents]]\nid = "coffee-spill"\nbase_rate = 0.4\n\n'
        '[[incidents]]\nid = "coffee-spill"\nbase_rate = 0.1\n',
    )

    with pytest.raises(ValidationError, match="duplicate incident id"):
        load_incident_table(world)


def test_load_incident_table_rejects_unknown_chain_trigger_target(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_table(
        world,
        '[[incidents]]\nid = "coffee-spill"\nbase_rate = 0.4\n'
        'chain_triggers = [{ target = "does-not-exist", mode = "direct" }]\n',
    )

    with pytest.raises(ValidationError, match="unknown target"):
        load_incident_table(world)


def test_evaluate_tick_is_reproducible_under_fixed_seed(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_table(world)
    table = load_incident_table(world)
    state = {"morale": 5}

    first = evaluate_tick(table, state=state, seed=4, tick=3)
    second = evaluate_tick(table, state=state, seed=4, tick=3)

    assert [cascade["members"] for cascade in first.cascades] == [
        cascade["members"] for cascade in second.cascades
    ]
    assert first.events == second.events
    assert [
        member["incident_id"] for cascade in first.cascades for member in cascade["members"]
    ] == ["coffee-spill", "printer-jam"]
    assert first.events != []


def test_evaluate_tick_skips_incidents_whose_preconditions_fail(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_table(world)
    table = load_incident_table(world)

    blocked = evaluate_tick(table, state={"morale": -5}, seed=4, tick=3)
    allowed = evaluate_tick(table, state={"morale": 5}, seed=4, tick=3)

    assert _fired_ids(blocked) == {"printer-jam"}
    assert _fired_ids(allowed) == {"coffee-spill", "printer-jam"}


def _fired_ids(resolution: IncidentResolution) -> set[str]:
    return {
        member["incident_id"] for cascade in resolution.cascades for member in cascade["members"]
    }


def test_evaluate_tick_produces_depth_two_cascade_as_single_event(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_table(world, CASCADE_TABLE)
    table = load_incident_table(world)

    resolution = evaluate_tick(table, state={}, seed=1, tick=1)

    assert len(resolution.cascades) == 1
    cascade = resolution.cascades[0]
    assert cascade["depth"] == 2
    assert [member["incident_id"] for member in cascade["members"]] == [
        "root-incident",
        "chained-incident",
        "boosted-incident",
    ]
    assert [member["trigger"] for member in cascade["members"]] == ["root", "direct", "boost"]

    incident_ids = {event["incident_id"] for event in resolution.events}
    assert incident_ids == {"root-incident", "chained-incident", "boosted-incident"}
    assert all(event["cascade_id"] == cascade["id"] for event in resolution.events)


def test_evaluate_tick_bounds_cascade_depth(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    build_chain_table(world, MAX_CASCADE_DEPTH + 3)
    table = load_incident_table(world)

    resolution = evaluate_tick(table, state={}, seed=7, tick=1)

    assert len(resolution.cascades) == 1
    cascade = resolution.cascades[0]
    depths = [member["depth"] for member in cascade["members"]]
    assert max(depths) == MAX_CASCADE_DEPTH
    assert len(cascade["members"]) == MAX_CASCADE_DEPTH + 1
    fired_ids = {member["incident_id"] for member in cascade["members"]}
    assert f"chain-{MAX_CASCADE_DEPTH + 1}" not in fired_ids
    assert f"chain-{MAX_CASCADE_DEPTH + 2}" not in fired_ids
