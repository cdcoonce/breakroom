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


@pytest.mark.parametrize(
    ("id_value", "display_value"),
    [("1", "1"), ("true", "True"), ('""', "''")],
)
def test_load_incident_table_rejects_non_string_or_empty_id(
    tmp_path: Path, id_value: str, display_value: str
) -> None:
    world = tmp_path / "tower"
    write_table(world, f"[[incidents]]\nid = {id_value}\nbase_rate = 0.4\n")

    with pytest.raises(ValidationError, match="incident id must be a non-empty string") as exc:
        load_incident_table(world)

    assert display_value in str(exc.value)


def test_load_incident_table_rejects_bool_int_id_collision(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_table(
        world,
        "[[incidents]]\nid = 1\nbase_rate = 0.4\n\n"
        "[[incidents]]\nid = true\nbase_rate = 0.2\n",
    )

    with pytest.raises(ValidationError, match="incident id must be a non-empty string") as exc:
        load_incident_table(world)

    assert "got 1" in str(exc.value)


@pytest.mark.parametrize(
    ("target_value", "display_value"),
    [("1", "1"), ("true", "True"), ("[]", "[]"), ('""', "''")],
)
def test_load_incident_table_rejects_non_string_or_empty_chain_target(
    tmp_path: Path, target_value: str, display_value: str
) -> None:
    world = tmp_path / "tower"
    write_table(
        world,
        "[[incidents]]\nid = \"coffee-spill\"\nbase_rate = 0.4\n"
        f"chain_triggers = [{{ target = {target_value}, mode = \"direct\" }}]\n",
    )

    with pytest.raises(
        ValidationError, match="chain_trigger target must be a non-empty string"
    ) as exc:
        load_incident_table(world)

    assert display_value in str(exc.value)


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


@pytest.mark.parametrize(
    ("path_value", "display_value"),
    [("123", "123"), ("[\"a\", \"b\"]", "['a', 'b']")],
)
def test_load_incident_table_rejects_non_string_precondition_path(
    tmp_path: Path, path_value: str, display_value: str
) -> None:
    world = tmp_path / "tower"
    write_table(
        world,
        '[[incidents]]\nid = "coffee-spill"\nbase_rate = 0.4\n'
        f"preconditions = [{{ path = {path_value}, operator = \"gte\", value = 0 }}]\n",
    )

    with pytest.raises(ValidationError, match="precondition path must be a string") as exc:
        load_incident_table(world)

    assert display_value in str(exc.value)


@pytest.mark.parametrize(
    ("operator_value", "display_value"),
    [("[]", "[]"), ("1", "1"), ("true", "True")],
)
def test_load_incident_table_rejects_non_string_precondition_operator(
    tmp_path: Path, operator_value: str, display_value: str
) -> None:
    world = tmp_path / "tower"
    write_table(
        world,
        '[[incidents]]\nid = "coffee-spill"\nbase_rate = 0.4\n'
        f"preconditions = [{{ path = \"morale\", operator = {operator_value}, value = 0 }}]\n",
    )

    with pytest.raises(
        ValidationError, match="precondition operator must be a string"
    ) as exc:
        load_incident_table(world)

    assert display_value in str(exc.value)


def test_load_incident_table_preserves_empty_precondition_path_policy(
    tmp_path: Path,
) -> None:
    world = tmp_path / "tower"
    write_table(
        world,
        '[[incidents]]\nid = "coffee-spill"\nbase_rate = 0.4\n'
        'preconditions = [{ path = "", operator = "eq", value = 0 }]\n',
    )

    table = load_incident_table(world)

    assert table.incidents["coffee-spill"].preconditions[0].path == ""


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


def test_load_incident_table_rejects_non_numeric_amount_in_direct_mode(
    tmp_path: Path,
) -> None:
    world = tmp_path / "tower"
    write_table(
        world,
        '[[incidents]]\nid = "coffee-spill"\nbase_rate = 0.4\n'
        '[[incidents]]\nid = "printer-jam"\nbase_rate = 0.2\n'
        'chain_triggers = [{ target = "coffee-spill", mode = "direct", amount = "oops" }]\n',
    )

    with pytest.raises(ValidationError, match="amount must be numeric"):
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


def test_evaluate_tick_reports_unorderable_gte_precondition_values(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_table(world)
    table = load_incident_table(world)

    with pytest.raises(ValidationError) as exc:
        evaluate_tick(table, state={"morale": "ready"}, seed=4, tick=3)

    message = str(exc.value)
    assert "morale" in message
    assert "gte" in message
    assert "ready" in message
    assert "0" in message


def test_evaluate_tick_reports_unorderable_lte_precondition_values(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_table(
        world,
        '[[incidents]]\nid = "coffee-spill"\nbase_rate = 1.0\n'
        'preconditions = [{ path = "morale", operator = "lte", value = 0 }]\n',
    )
    table = load_incident_table(world)

    with pytest.raises(ValidationError) as exc:
        evaluate_tick(table, state={"morale": "ready"}, seed=4, tick=3)

    message = str(exc.value)
    assert "morale" in message
    assert "lte" in message
    assert "ready" in message
    assert "0" in message


@pytest.mark.parametrize(
    ("operator", "state_value", "configured_value", "expected_fired"),
    [
        ("gte", 5, "0", True),
        ("gte", -5, "0", False),
        ("lte", -5, "0", True),
        ("lte", 5, "0", False),
        ("gte", "ready", '"m"', True),
        ("lte", "ready", '"z"', True),
    ],
)
def test_evaluate_tick_preserves_comparable_precondition_results(
    tmp_path: Path,
    operator: str,
    state_value: int | str,
    configured_value: str,
    expected_fired: bool,
) -> None:
    world = tmp_path / "tower"
    write_table(
        world,
        '[[incidents]]\nid = "coffee-spill"\nbase_rate = 1.0\n'
        f'preconditions = [{{ path = "morale", operator = "{operator}", '
        f"value = {configured_value} }}]\n",
    )
    table = load_incident_table(world)

    resolution = evaluate_tick(table, state={"morale": state_value}, seed=4, tick=3)

    assert _fired_ids(resolution) == ({"coffee-spill"} if expected_fired else set())


@pytest.mark.parametrize(
    ("path", "operator", "state", "expected_fired"),
    [
        ("morale", "eq", {"morale": 5}, True),
        ("morale", "eq", {"morale": 6}, False),
        ("unresolved.value", "gte", {}, False),
    ],
)
def test_evaluate_tick_preserves_eq_and_missing_path_behavior(
    tmp_path: Path,
    path: str,
    operator: str,
    state: dict[str, int],
    expected_fired: bool,
) -> None:
    world = tmp_path / "tower"
    write_table(
        world,
        '[[incidents]]\nid = "coffee-spill"\nbase_rate = 1.0\n'
        f'preconditions = [{{ path = "{path}", operator = "{operator}", value = 5 }}]\n',
    )
    table = load_incident_table(world)

    resolution = evaluate_tick(table, state=state, seed=4, tick=3)

    assert _fired_ids(resolution) == ({"coffee-spill"} if expected_fired else set())


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


def test_evaluate_tick_emits_diamond_incident_once_per_cascade(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_table(
        world,
        '''
[[incidents]]
id = "a-root"
base_rate = 1.0
chain_triggers = [
  { target = "b-left", mode = "direct" },
  { target = "c-right", mode = "direct" },
]

[[incidents]]
id = "b-left"
base_rate = 0.0
chain_triggers = [{ target = "d-shared", mode = "direct" }]

[[incidents]]
id = "c-right"
base_rate = 0.0
chain_triggers = [{ target = "d-shared", mode = "direct" }]

[[incidents]]
id = "d-shared"
base_rate = 1.0
effects = [{ type = "dial_delta", dials = { reputation = 2 } }]
''',
    )
    table = load_incident_table(world)

    first = evaluate_tick(table, state={}, seed=19, tick=7)
    second = evaluate_tick(table, state={}, seed=19, tick=7)

    assert len(first.cascades) == 2
    diamond, independent_root = first.cascades
    assert [member["incident_id"] for member in diamond["members"]] == [
        "a-root",
        "b-left",
        "d-shared",
        "c-right",
    ]
    assert [member["trigger"] for member in diamond["members"]] == [
        "root",
        "direct",
        "direct",
        "direct",
    ]
    assert [member["incident_id"] for member in independent_root["members"]] == [
        "d-shared"
    ]
    d_effects = [event for event in first.events if event["incident_id"] == "d-shared"]
    assert [event["cascade_id"] for event in d_effects] == [diamond["id"], independent_root["id"]]
    assert first.cascades == second.cascades
    assert first.events == second.events


def test_evaluate_tick_emits_mutual_cycle_once_per_cascade(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_table(
        world,
        '''
[[incidents]]
id = "a-root"
base_rate = 1.0
effects = [{ type = "dial_delta", dials = { morale = -1 } }]
chain_triggers = [{ target = "b-child", mode = "direct" }]

[[incidents]]
id = "b-child"
base_rate = 0.0
effects = [{ type = "dial_delta", dials = { reputation = -2 } }]
chain_triggers = [{ target = "a-root", mode = "direct" }]
''',
    )
    table = load_incident_table(world)

    first = evaluate_tick(table, state={}, seed=19, tick=7)
    second = evaluate_tick(table, state={}, seed=19, tick=7)

    assert len(first.cascades) == 1
    cascade = first.cascades[0]
    assert [
        (member["incident_id"], member["depth"], member["trigger"])
        for member in cascade["members"]
    ] == [("a-root", 0, "root"), ("b-child", 1, "direct")]
    assert [
        (event["incident_id"], event["depth"], event["dials"])
        for event in first.events
    ] == [
        ("a-root", 0, {"morale": -1}),
        ("b-child", 1, {"reputation": -2}),
    ]
    assert first.cascades == second.cascades
    assert first.events == second.events


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
