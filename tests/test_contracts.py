from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from breakroom.cli import main
from breakroom.init import init_world
from breakroom.worldstate import ValidationError, load_world, replay_events


def economy_api():
    return importlib.import_module("breakroom.economy")


def _world(tmp_path: Path, *, seed: int = 42, reputation: int = 100) -> Path:
    world = tmp_path / "tower"
    init_world(world, seed=seed)
    state_path = world / "state" / "tower.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["reputation"] = reputation
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    incidents = world / "data" / "incidents.toml"
    incidents.write_text(
        incidents.read_text(encoding="utf-8").replace("base_rate = 1.0", "base_rate = 0.0"),
        encoding="utf-8",
    )
    return world


def _read_events(world: Path) -> list[dict]:
    return [json.loads(line) for line in (world / "events.jsonl").read_text().splitlines()]


def _offers(world: Path) -> list[dict]:
    return [event for event in _read_events(world) if event["type"] == "contract_offer"]


def _standard_override(**changes: str) -> str:
    values = {
        "offer_probability_per_reputation_point": "0.01",
        "offer_lifetime_ticks": "3",
        "matching_room_factor": "1.0",
        "mismatching_room_factor": "0.5",
    }
    values.update(changes)
    return (
        "offer_probability_per_reputation_point = "
        f"{values['offer_probability_per_reputation_point']}\n"
        f"offer_lifetime_ticks = {values['offer_lifetime_ticks']}\n"
        f"matching_room_factor = {values['matching_room_factor']}\n"
        f"mismatching_room_factor = {values['mismatching_room_factor']}\n"
        '\n[templates.standard]\nclient = "Aperture Office Supply"\n'
        'required_work_units = 3\nduration_ticks = 3\nrequired_room_kind = "work"\n'
        'payout_budget = 40\nmiss_penalty_budget = 20\nmiss_penalty_reputation = 5\n'
        'pressure_milestones = [{ ticks_remaining = 2, level = "watch" }, '
        '{ ticks_remaining = 1, level = "urgent" }, { ticks_remaining = 0, level = "due" }]\n'
    )


def _duration_pressure_override() -> str:
    default = (
        'pressure_milestones = [{ ticks_remaining = 2, level = "watch" }, '
        '{ ticks_remaining = 1, level = "urgent" }, '
        '{ ticks_remaining = 0, level = "due" }]'
    )
    return _standard_override().replace(
        default,
        'pressure_milestones = [{ ticks_remaining = 3, level = "due" }]',
    )


def test_new_world_copies_contract_config_and_bundled_template_is_explicit(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=42)

    config_path = world / "data" / "contracts.toml"
    assert config_path.is_file()
    config = economy_api().load_contract_config(world)
    standard = config["templates"]["standard"]
    assert config["offer_probability_per_reputation_point"] == 0.01
    assert config["offer_lifetime_ticks"] == 3
    assert standard == {
        "client": "Aperture Office Supply",
        "required_work_units": 3,
        "duration_ticks": 3,
        "required_room_kind": "work",
        "payout_budget": 40,
        "miss_penalty_budget": 20,
        "miss_penalty_reputation": 5,
        "pressure_milestones": [
            {"ticks_remaining": 2, "level": "watch"},
            {"ticks_remaining": 1, "level": "urgent"},
            {"ticks_remaining": 0, "level": "due"},
        ],
    }
    assert config["matching_room_factor"] == 1.0
    assert config["mismatching_room_factor"] == 0.5


def test_old_world_uses_bundled_contracts_and_defaults_ignore_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    world = _world(tmp_path)
    override = world / "data" / "contracts.toml"
    override.unlink(missing_ok=True)
    decoy = tmp_path / "cwd" / "data"
    decoy.mkdir(parents=True)
    (decoy / "contracts.toml").write_text("not = [valid", encoding="utf-8")
    monkeypatch.chdir(decoy.parent)

    config = economy_api().load_contract_config(world)

    assert config["templates"]["standard"]["required_work_units"] == 3


def test_present_valid_contract_override_takes_precedence(tmp_path: Path) -> None:
    world = _world(tmp_path)
    override = world / "data" / "contracts.toml"
    override.write_text(
        _standard_override().replace("payout_budget = 40", "payout_budget = 77"),
        encoding="utf-8",
    )
    assert (
        economy_api().load_contract_config(world)["templates"]["standard"]["payout_budget"]
        == 77
    )


@pytest.mark.parametrize(
    "contents",
    [
        "[broken\n",
        _standard_override(offer_probability_per_reputation_point="true"),
        _standard_override(offer_probability_per_reputation_point="0.02"),
        _standard_override(offer_lifetime_ticks="true"),
        _standard_override(matching_room_factor="0.9"),
        _standard_override(mismatching_room_factor="nan"),
        _standard_override().replace("required_work_units = 3", "required_work_units = true"),
        _standard_override().replace("required_work_units = 3", "required_work_units = nan"),
        _standard_override().replace("duration_ticks = 3", "duration_ticks = true"),
        _standard_override().replace("payout_budget = 40", "payout_budget = true"),
        _standard_override().replace("payout_budget = 40", "payout_budget = nan"),
        _duration_pressure_override(),
        _standard_override().replace(
            "ticks_remaining = 1, level = \"urgent\"",
            "ticks_remaining = 2, level = \"urgent\"",
        ),
        _standard_override().replace(
            "ticks_remaining = 1, level = \"urgent\"",
            "ticks_remaining = 1, level = \"watch\"",
        ),
    ],
)
def test_present_invalid_contract_override_fails_closed(
    tmp_path: Path, contents: str
) -> None:
    world = _world(tmp_path)
    (world / "data" / "contracts.toml").write_text(contents, encoding="utf-8")

    with pytest.raises(ValidationError, match="contracts.toml"):
        economy_api().load_contract_config(world)


def test_contract_override_rejects_non_file_and_dangling_symlink(tmp_path: Path) -> None:
    api = economy_api()
    world = _world(tmp_path)
    config_path = world / "data" / "contracts.toml"
    config_path.unlink(missing_ok=True)
    config_path.mkdir()
    with pytest.raises(ValidationError, match="contracts.toml"):
        api.load_contract_config(world)

    config_path.rmdir()
    config_path.symlink_to(tmp_path / "missing-contracts.toml")
    with pytest.raises(ValidationError, match="contracts.toml"):
        api.load_contract_config(world)


def test_fixed_seed_offer_draws_use_the_dedicated_stream_and_snapshot_terms(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path, seed=42, reputation=50)

    for _ in range(10):
        from breakroom.tick import tick_world

        tick_world(world)

    offers = _offers(world)
    assert [event["created_day"] for event in offers] == [3, 7, 10]
    assert [event["offer_id"] for event in offers] == [
        "contract-offer-0003",
        "contract-offer-0007",
        "contract-offer-0010",
    ]
    assert all(event["expires_day"] == event["created_day"] + 3 for event in offers)
    assert all(event["terms"]["required_work_units"] == 3 for event in offers)
    by_day = {
        event["day"]: event["rolls"]
        for event in _read_events(world)
        if event["type"] == "quiet_day"
    }
    contract_rolls = [
        next(record for record in by_day[day] if record["purpose"] == "contract_offer")
        for day in range(1, 11)
    ]
    assert [record["stream"] for record in contract_rolls] == ["contract_offers"] * 10
    assert [record["result"] for record in contract_rolls] == [
        False,
        False,
        True,
        False,
        False,
        False,
        True,
        False,
        False,
        True,
    ]


def test_offer_probability_scales_from_zero_to_reputation_ceiling(tmp_path: Path) -> None:
    from breakroom.tick import tick_world

    worlds = []
    for reputation in (0, 100):
        world = _world(tmp_path / str(reputation), reputation=reputation)
        for _ in range(3):
            tick_world(world)
        worlds.append(_offers(world))
    assert worlds[0] == []
    assert [event["offer_id"] for event in worlds[1]] == [
        "contract-offer-0001",
        "contract-offer-0002",
        "contract-offer-0003",
    ]


def test_contract_list_accept_decline_are_durable_and_replayable(tmp_path: Path, capsys) -> None:
    world = _world(tmp_path)
    from breakroom.tick import tick_world

    initial = json.loads(json.dumps(load_world(world).state))
    tick_world(world)
    offer_id = _offers(world)[0]["offer_id"]

    assert main(["contracts", "list", "--world", str(world)]) == 0
    assert offer_id in capsys.readouterr().out
    accepted = economy_api().accept_contract(world, offer_id, ["jordan-vale"])
    assert accepted["status"] == "accepted"
    after_accept = load_world(world).state
    assert after_accept["contracts"][offer_id]["status"] == "accepted"
    assert after_accept["contracts"][offer_id]["work_room_id"] == "open-office"
    assert main(["contracts", "list", "--world", str(world)]) == 0
    assert "accepted" in capsys.readouterr().out
    accepted_event = [
        event for event in _read_events(world) if event["type"] == "contract_accepted"
    ][0]
    assert accepted_event["contract_id"] == offer_id

    tick_world(world)
    next_offer = _offers(world)[-1]["offer_id"]
    declined = economy_api().decline_contract(world, next_offer)
    assert declined["status"] == "declined"
    assert load_world(world).state["contracts"][next_offer]["status"] == "declined"
    assert replay_events(initial, world / "events.jsonl") == load_world(world).state


def test_invalid_accept_writes_neither_journal_nor_snapshot(tmp_path: Path) -> None:
    world = _world(tmp_path)
    from breakroom.tick import tick_world

    tick_world(world)
    offer_id = _offers(world)[0]["offer_id"]
    events_path = world / "events.jsonl"
    snapshot_path = world / "state" / "tower.json"
    events_before, snapshot_before = events_path.read_bytes(), snapshot_path.read_bytes()

    with pytest.raises(ValidationError):
        economy_api().accept_contract(world, offer_id, ["jordan-vale", "jordan-vale"])

    assert events_path.read_bytes() == events_before
    assert snapshot_path.read_bytes() == snapshot_before


@pytest.mark.parametrize("focus", ["true", "-1"])
def test_accept_rejects_invalid_focus_without_writes(tmp_path: Path, focus: str) -> None:
    world = _world(tmp_path)
    from breakroom.tick import tick_world

    tick_world(world)
    offer_id = _offers(world)[0]["offer_id"]
    character_path = world / "characters" / "jordan-vale.toml"
    character_path.write_text(
        character_path.read_text(encoding="utf-8").replace("focus = 2", f"focus = {focus}"),
        encoding="utf-8",
    )
    events_path = world / "events.jsonl"
    snapshot_path = world / "state" / "tower.json"
    before = events_path.read_bytes(), snapshot_path.read_bytes()
    with pytest.raises(ValidationError, match="focus"):
        economy_api().accept_contract(world, offer_id, ["jordan-vale"])
    assert (events_path.read_bytes(), snapshot_path.read_bytes()) == before


def test_offer_is_acceptible_before_expiry_and_expires_on_the_expiry_day(tmp_path: Path) -> None:
    before_world = _world(tmp_path / "before")
    from breakroom.tick import tick_world

    tick_world(before_world)
    offer_id = _offers(before_world)[0]["offer_id"]
    tick_world(before_world)
    tick_world(before_world)
    assert load_world(before_world).state["day"] == 3
    accepted = economy_api().accept_contract(before_world, offer_id, ["jordan-vale"])
    assert accepted["status"] == "accepted"

    expired_world = _world(tmp_path / "expired")
    tick_world(expired_world)
    expired_id = _offers(expired_world)[0]["offer_id"]
    tick_world(expired_world)
    tick_world(expired_world)
    tick_world(expired_world)
    expired_events = [
        event
        for event in _read_events(expired_world)
        if event.get("contract_id", event.get("offer_id")) == expired_id and event["day"] == 4
    ]
    assert [event["type"] for event in expired_events] == ["contract_expired"]
    with pytest.raises(ValidationError, match="expired"):
        economy_api().accept_contract(expired_world, expired_id, ["jordan-vale"])


@pytest.mark.parametrize(
    ("team", "room_id"),
    [
        ([], None),
        (["unknown-character"], None),
        (["jordan-vale", "jordan-vale"], None),
        (["jordan-vale"], "missing-room"),
    ],
)
def test_accept_rejects_invalid_team_or_room_without_partial_event(
    tmp_path: Path, team: list[str], room_id: str | None
) -> None:
    world = _world(tmp_path)
    from breakroom.tick import tick_world

    tick_world(world)
    offer_id = _offers(world)[0]["offer_id"]
    events_path = world / "events.jsonl"
    snapshot_path = world / "state" / "tower.json"
    event_bytes = events_path.read_bytes()
    snapshot_bytes = snapshot_path.read_bytes()

    with pytest.raises(ValidationError):
        economy_api().accept_contract(world, offer_id, team, work_room_id=room_id)

    assert events_path.read_bytes() == event_bytes
    assert snapshot_path.read_bytes() == snapshot_bytes


def test_cli_accept_decline_contract_commands_need_no_model(tmp_path: Path, capsys) -> None:
    world = _world(tmp_path)
    from breakroom.tick import tick_world

    initial = json.loads(json.dumps(load_world(world).state))
    tick_world(world)
    offer_id = _offers(world)[0]["offer_id"]
    assert main(["contracts", "list", "--world", str(world)]) == 0
    listing = capsys.readouterr().out
    assert "Aperture Office Supply" in listing
    assert "required_work_units" in listing
    assert "expires_day" in listing
    assert main(
        ["contracts", "accept", offer_id, "--team", "jordan-vale", "--world", str(world)]
    ) == 0
    assert "accepted" in capsys.readouterr().out
    accepted = load_world(world).state["contracts"][offer_id]
    assert accepted["status"] == "accepted"
    assert accepted["deadline_day"] == 4
    assert accepted["team_ids"] == ["jordan-vale"]
    assert accepted["work_room_id"] == "open-office"
    assert main(["contracts", "list", "--world", str(world)]) == 0
    listing = capsys.readouterr().out
    assert "deadline_day" in listing
    assert "work_room_id" in listing
    assert '"progress": 0' in listing
    tick_world(world)
    after_work = load_world(world).state["contracts"][offer_id]
    assert after_work["progress"] == 2
    assert after_work["last_work"]["team_focus"] == {"jordan-vale": 2}
    next_offer = _offers(world)[-1]["offer_id"]
    assert main(["contracts", "decline", next_offer, "--world", str(world)]) == 0
    assert "declined" in capsys.readouterr().out
    assert load_world(world).state["contracts"][next_offer]["status"] == "declined"
    assert replay_events(initial, world / "events.jsonl") == load_world(world).state


def test_installed_wheel_loads_bundled_contract_config_outside_checkout(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_dir.glob("breakroom-*.whl"))
    with ZipFile(wheel) as archive:
        assert "breakroom/data/contracts.toml" in archive.namelist()
    installed = tmp_path / "installed"
    installed.mkdir()
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(installed),
            "--no-deps",
            str(wheel),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(installed)
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "from pathlib import Path; from breakroom.economy import load_contract_config; "
            "print(load_contract_config(Path('/no/world'))['templates']['standard']['client'])",
        ],
        cwd=outside,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "Aperture Office Supply"
