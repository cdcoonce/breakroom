from __future__ import annotations

import json
from pathlib import Path

import pytest

from breakroom import economy, jsonio, worldstate
from breakroom.init import STARTER_INCIDENTS, init_world
from breakroom.tick import tick_world
from breakroom.worldstate import ValidationError, load_world, replay_events


def _world(tmp_path: Path, *, focus_values: tuple[int, ...] = (2,)) -> Path:
    world = tmp_path / "tower"
    init_world(world, seed=42)
    state_path = world / "state" / "tower.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["reputation"] = 100
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    character_path = world / "characters" / "jordan-vale.toml"
    character = character_path.read_text(encoding="utf-8")
    character_path.write_text(
        character.replace("focus = 2", f"focus = {focus_values[0]}"), encoding="utf-8"
    )
    for index, focus in enumerate(focus_values[1:], start=2):
        character_id = f"worker-{index}"
        state["characters"].append(character_id)
        (world / "characters" / f"{character_id}.toml").write_text(
            f'id = "{character_id}"\nname = "Worker {index}"\nmodel = "test-model"\n'
            f"[stats]\nfocus = {focus}\nempathy = 1\nnerve = 1\n",
            encoding="utf-8",
        )
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    incident_path = world / "data" / "incidents.toml"
    incident_path.write_text(
        incident_path.read_text(encoding="utf-8").replace("base_rate = 1.0", "base_rate = 0.0"),
        encoding="utf-8",
    )
    return world


def _events(world: Path) -> list[dict]:
    return [json.loads(line) for line in (world / "events.jsonl").read_text().splitlines()]


def _disable_future_offers(world: Path) -> None:
    path = world / "data" / "contracts.toml"
    path.write_text(_contract_config(rate=0.0), encoding="utf-8")


def _contract_config(*, rate: float = 0.01, payout: int = 40) -> str:
    return (
        f"offer_probability_per_reputation_point = {rate}\n"
        "offer_lifetime_ticks = 3\nmatching_room_factor = 1.0\n"
        "mismatching_room_factor = 0.5\n\n[templates.standard]\n"
        'client = "Aperture Office Supply"\nrequired_work_units = 3\n'
        'duration_ticks = 3\nrequired_room_kind = "work"\n'
        f"payout_budget = {payout}\nmiss_penalty_budget = 20\n"
        "miss_penalty_reputation = 5\n"
        'pressure_milestones = [{ ticks_remaining = 2, level = "watch" }, '
        '{ ticks_remaining = 1, level = "urgent" }, { ticks_remaining = 0, level = "due" }]\n'
    )


def _start_offer(world: Path) -> str:
    tick_world(world)
    offer = next(event for event in _events(world) if event["type"] == "contract_offer")
    _disable_future_offers(world)
    return offer["offer_id"]


@pytest.mark.parametrize(
    ("focus_values", "room_id", "deltas", "totals", "terminal", "pressure"),
    [
        ((2,), "open-office", [2, 2], [2, 4], "completed", ["watch"]),
        ((2,), "break-room", [1, 1, 1], [1, 2, 3], "completed", ["watch", "urgent"]),
        ((0,), "open-office", [0, 0, 0], [0, 0, 0], "missed", ["watch", "urgent", "due"]),
        ((2, 3), "open-office", [5], [5], "completed", []),
    ],
)
def test_worked_progress_examples_pin_receipts_and_terminal_order(
    tmp_path: Path,
    focus_values: tuple[int, ...],
    room_id: str,
    deltas: list[int],
    totals: list[int],
    terminal: str,
    pressure: list[str],
) -> None:
    world = _world(tmp_path, focus_values=focus_values)
    initial = json.loads(json.dumps(load_world(world).state))
    offer_id = _start_offer(world)
    team = ["jordan-vale", *[f"worker-{i}" for i in range(2, len(focus_values) + 1)]]
    accepted = economy.accept_contract(world, offer_id, team, work_room_id=room_id)
    assert accepted["deadline_day"] == 4

    for _ in deltas:
        tick_world(world)

    events = _events(world)
    progress = [event for event in events if event["type"] == "contract_progress"]
    assert [event["work_delta"] for event in progress] == deltas
    assert [event["progress"] for event in progress] == totals
    assert all(event["work_room_id"] == room_id for event in progress)
    assert all(event["required_room_kind"] == "work" for event in progress)
    assert all(
        event["fit_factor"] == (1.0 if room_id == "open-office" else 0.5)
        for event in progress
    )
    assert all(
        event["team_focus"] == dict(zip(team, focus_values, strict=True))
        for event in progress
    )
    pressure_events = [event for event in events if event["type"] == "contract_pressure"]
    assert [event["level"] for event in pressure_events] == pressure
    record = load_world(world).state["contracts"][offer_id]
    assert record["status"] == terminal
    settlement_types = {
        "contract_completion": "contract_completed",
        "contract_miss": "contract_missed",
    }
    settlement_sources = [
        event["source"]
        for event in events
        if event.get("contract_id") == offer_id
        and event.get("source") in settlement_types
    ]
    assert len(settlement_sources) == 1
    terminal_event_type = settlement_types[settlement_sources[0]]
    tick_world(world)
    later_events = _events(world)
    assert len(
        [
            event
            for event in later_events
            if event.get("contract_id") == offer_id
            and event.get("source") in settlement_types
        ]
    ) == 1
    assert load_world(world).state["contracts"][offer_id]["status"] == terminal
    assert replay_events(initial, world / "events.jsonl") == load_world(world).state
    assert len(
        [
            event
            for event in later_events
            if event.get("contract_id") == offer_id and event["type"] == terminal_event_type
        ]
    ) == 1

    contract_day_events = [
        event
        for event in events
        if event.get("contract_id") == offer_id and event["type"] != "contract_offer"
    ]
    last_day = len(deltas) + 1
    last_contract_events = [event for event in contract_day_events if event["day"] == last_day]
    if terminal == "completed":
        assert [event["type"] for event in last_contract_events] == [
            "contract_progress",
            "dial_delta",
            "contract_completed",
        ]
        settlement = last_contract_events[1]
        assert settlement["source"] == "contract_completion"
        assert settlement["amount"] == 40
        assert settlement["dial_movement"]["dials"] == {"budget": 40}
    else:
        assert [event["type"] for event in last_contract_events] == [
            "contract_progress",
            "contract_pressure",
            "dial_delta",
            "contract_missed",
        ]
        assert last_contract_events[1]["level"] == "due"
        settlement = last_contract_events[2]
        assert settlement["source"] == "contract_miss"
        assert settlement["dial_movement"]["dials"] == {"budget": -20, "reputation": -5}


def test_default_room_selection_and_frozen_offer_terms_survive_config_change(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path)
    offer_id = _start_offer(world)
    config_path = world / "data" / "contracts.toml"
    config_path.write_text(_contract_config(payout=999), encoding="utf-8")

    accepted = economy.accept_contract(world, offer_id, ["jordan-vale"])

    assert accepted["work_room_id"] == "open-office"
    assert accepted["terms"]["payout_budget"] == 40
    assert accepted["terms"]["required_work_units"] == 3


def test_declined_or_expired_offer_cannot_be_accepted(tmp_path: Path) -> None:
    world = _world(tmp_path)
    offer_id = _start_offer(world)
    economy.decline_contract(world, offer_id)
    events_before = (world / "events.jsonl").read_bytes()
    state_before = (world / "state" / "tower.json").read_bytes()

    with pytest.raises(ValidationError):
        economy.accept_contract(world, offer_id, ["jordan-vale"])
    assert (world / "events.jsonl").read_bytes() == events_before
    assert (world / "state" / "tower.json").read_bytes() == state_before


def test_contract_reducers_replay_frozen_work_and_do_not_advance_day() -> None:
    initial = {"day": 4, "budget": 0, "morale": 50, "reputation": 50, "rooms": [], "characters": []}
    offered = {
        "type": "contract_offer",
        "day": 4,
        "offer_id": "contract-offer-0004",
        "created_day": 4,
        "expires_day": 7,
        "client": "Client",
        "terms": {
            "required_work_units": 1,
            "duration_ticks": 3,
            "required_room_kind": "work",
            "payout_budget": 40,
            "miss_penalty_budget": 20,
            "miss_penalty_reputation": 5,
            "pressure_milestones": [],
        },
    }

    state = worldstate.apply_event(initial, offered)
    assert state["day"] == 4
    accepted = worldstate.apply_event(
        state,
        {
            "type": "contract_accepted",
            "day": 4,
            "contract_id": offered["offer_id"],
            "team_ids": ["worker"],
            "work_room_id": "room",
            "terms": offered["terms"],
        },
    )
    assert accepted["day"] == 4
    progress_event = {
        "type": "contract_progress",
        "day": 5,
        "contract_id": offered["offer_id"],
        "work_delta": 1,
        "progress": 1,
        "team_focus": {"worker": 2},
        "work_room_id": "room",
        "work_room_kind": "work",
        "required_room_kind": "work",
        "fit_factor": 1,
    }
    progressed = worldstate.apply_event(accepted, progress_event)
    assert progressed["day"] == 4
    assert progressed["contracts"][offered["offer_id"]]["progress"] == 1

    pressure = {
        "type": "contract_pressure",
        "day": 5,
        "contract_id": offered["offer_id"],
        "ticks_remaining": 2,
        "level": "watch",
    }
    pressured = worldstate.apply_event(progressed, pressure)
    with pytest.raises(ValidationError, match="duplicate or invalid pressure"):
        worldstate.apply_event(pressured, pressure)


def test_replay_uses_frozen_work_and_settlement_after_sources_change(tmp_path: Path) -> None:
    world = _world(tmp_path)
    initial = json.loads(json.dumps(load_world(world).state))
    offer_id = _start_offer(world)
    economy.accept_contract(world, offer_id, ["jordan-vale"])
    _disable_future_offers(world)
    tick_world(world)
    saved = load_world(world).state

    character_path = world / "characters" / "jordan-vale.toml"
    character_path.write_text(
        character_path.read_text(encoding="utf-8").replace("focus = 2", "focus = 999"),
        encoding="utf-8",
    )
    (world / "data" / "contracts.toml").write_text("malformed [", encoding="utf-8")
    (world / "data" / "economy.toml").write_text("malformed [", encoding="utf-8")

    assert replay_events(initial, world / "events.jsonl") == saved


def test_narrator_failure_discards_contract_receipts_and_retry_commits_once(
    tmp_path: Path, monkeypatch
) -> None:
    world = _world(tmp_path)
    initial = json.loads(json.dumps(load_world(world).state))
    offer_id = _start_offer(world)
    economy.accept_contract(world, offer_id, ["jordan-vale"])
    _disable_future_offers(world)
    (world / "data" / "incidents.toml").write_text(STARTER_INCIDENTS, encoding="utf-8")
    events_path = world / "events.jsonl"
    state_path = world / "state" / "tower.json"
    events_before = events_path.read_bytes()
    state_before = state_path.read_bytes()
    chronicles_before = sorted((world / "chronicles").glob("*.md"))
    brief_seen = []

    def fail(brief: dict) -> str:
        brief_seen.append(brief)
        raise RuntimeError("narrator offline")

    monkeypatch.setattr("breakroom.tick.render_scene", fail)
    with pytest.raises(RuntimeError, match="narrator offline"):
        tick_world(world)
    assert events_path.read_bytes() == events_before
    assert state_path.read_bytes() == state_before
    assert sorted((world / "chronicles").glob("*.md")) == chronicles_before
    assert brief_seen and "contracts" in brief_seen[0]

    monkeypatch.setattr("breakroom.tick.render_scene", lambda _brief: "retry scene")
    tick_world(world)
    events = _events(world)
    assert len([event for event in events if event["type"] == "contract_progress"]) == 1
    assert replay_events(initial, world / "events.jsonl") == load_world(world).state


def test_contract_phase_precedes_incidents_and_payroll_remains_first(tmp_path: Path) -> None:
    world = _world(tmp_path)
    offer_id = _start_offer(world)
    economy.accept_contract(world, offer_id, ["jordan-vale"])
    _disable_future_offers(world)
    (world / "data" / "incidents.toml").write_text(STARTER_INCIDENTS, encoding="utf-8")
    tick_world(world)

    day_two = [event for event in _events(world) if event.get("day") == 2]
    assert day_two[0]["type"] == "dial_delta" and day_two[0]["source"] == "payroll"
    progress_index = next(
        i for i, event in enumerate(day_two) if event["type"] == "contract_progress"
    )
    first_incident_index = next(i for i, event in enumerate(day_two) if event["type"] == "incident")
    assert progress_index < first_incident_index
    assert day_two[-1]["type"] in {"scene", "quiet_day"}


def test_contract_draw_does_not_perturb_incident_or_storylet_rng(
    tmp_path: Path, monkeypatch
) -> None:
    worlds = []
    for rate in (0.0, 0.01):
        world = tmp_path / str(rate)
        init_world(world, seed=42)
        state_path = world / "state" / "tower.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["reputation"] = 100
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (world / "data" / "contracts.toml").write_text(
            _contract_config(rate=rate), encoding="utf-8"
        )
        worlds.append(world)

    monkeypatch.setattr("breakroom.tick.render_scene", lambda _brief: "scene")
    for world in worlds:
        tick_world(world)
    event_sets = [_events(world) for world in worlds]
    roll_sets = [
        next(event["rolls"] for event in events if event["type"] == "scene")
        for events in event_sets
    ]
    def filter_rng(rolls: list[dict]) -> list[dict]:
        return [record for record in rolls if record["stream"] != "contract_offers"]

    assert filter_rng(roll_sets[0]) == filter_rng(roll_sets[1])
    incident_views = [
        [
            {key: value for key, value in event.items() if key != "sequence"}
            for event in events
            if event["type"] == "incident"
        ]
        for events in event_sets
    ]
    assert incident_views[0] == incident_views[1]
    scenes = [next(event for event in events if event["type"] == "scene") for events in event_sets]
    assert (scenes[0]["storylet_id"], scenes[0]["character_ids"]) == (
        scenes[1]["storylet_id"],
        scenes[1]["character_ids"],
    )


def test_quiet_tick_persists_contracts_without_calling_narrator(
    tmp_path: Path, monkeypatch
) -> None:
    world = _world(tmp_path)

    def fail(_brief: dict) -> str:
        raise AssertionError("quiet contract ticks do not call the narrator")

    monkeypatch.setattr("breakroom.tick.render_scene", fail)
    tick_world(world)
    offer = next(event for event in _events(world) if event["type"] == "contract_offer")
    economy.accept_contract(world, offer["offer_id"], ["jordan-vale"])
    tick_world(world)

    day_two = [event for event in _events(world) if event.get("day") == 2]
    assert day_two[0]["source"] == "payroll"
    assert [event["type"] for event in day_two[1:3]] == [
        "contract_offer",
        "contract_progress",
    ]
    assert day_two[-1]["type"] == "quiet_day"
    assert load_world(world).state["contracts"][offer["offer_id"]]["progress"] == 2


def test_no_selected_storylet_persists_contract_receipts_without_narrator(
    tmp_path: Path, monkeypatch
) -> None:
    world = _world(tmp_path)
    (world / "data" / "incidents.toml").write_text(
        """[[incidents]]
id = "unmatched-incident"
base_rate = 1.0
rooms = ["break-room"]
[[incidents.effects]]
type = "incident_detail"
name = "Unmatched incident"
room = "break-room"
morale_delta = -1
norm_tags = []
needs_cleanup = false
""",
        encoding="utf-8",
    )

    def fail(_brief: dict) -> str:
        raise AssertionError("no-selected-storylet ticks do not call the narrator")

    monkeypatch.setattr("breakroom.tick.render_scene", fail)
    tick_world(world)
    events = _events(world)
    assert events[0]["source"] == "payroll"
    assert events[1]["type"] == "contract_offer"
    assert events[2]["type"] == "incident"
    assert events[-1]["type"] == "quiet_day"


def test_old_world_without_contract_registry_loads_as_empty(tmp_path: Path) -> None:
    world = _world(tmp_path)
    state_path = world / "state" / "tower.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.pop("contracts")
    state["reputation"] = 0
    jsonio.write_pretty_json(state_path, state)
    (world / "data" / "contracts.toml").unlink()
    initial = json.loads(json.dumps(state))
    original_initial = json.loads(json.dumps(initial))

    assert load_world(world).state["contracts"] == {}
    assert replay_events(initial, world / "events.jsonl") == load_world(world).state
    assert initial == original_initial
    tick_world(world)
    assert replay_events(initial, world / "events.jsonl") == load_world(world).state
    assert initial == original_initial
