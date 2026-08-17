import json
from pathlib import Path

import pytest

from breakroom import norms
from breakroom.cli import main


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def events_of(world: Path, event_type: str) -> list[dict]:
    return [event for event in read_jsonl(world / "events.jsonl") if event["type"] == event_type]


@pytest.fixture
def stub_narrator(monkeypatch) -> None:
    """Norm tagging is computed, never narrated — keep the model out of these ticks."""

    def render_scene(brief: dict) -> str:
        return f"{brief['character']['name']} faced {brief['incident']['name']}."

    monkeypatch.setattr("breakroom.tick.render_scene", render_scene)


def tick_once(world: Path, seed: int) -> None:
    assert main(["init", "--world", str(world), "--seed", str(seed)]) == 0
    assert main(["tick", "--world", str(world)]) == 0


def incident_by_id(world: Path, incident_id: str) -> dict:
    return next(
        event for event in events_of(world, "incident") if event["incident"]["id"] == incident_id
    )


# base_rate = 1.0 on every starter incident, so all three fire on every tick regardless
# of seed; the seed only decides which storylet gets the spotlight draw and thus which
# incident the scene, brief, and chronicle are about. Integrity drift is a mechanic
# rather than presentation, so it covers every fired incident's violations however the
# draw lands. Seed 1 spotlights coffee-spill (needs_cleanup); seed 5 also spotlights
# coffee-spill — `quiet-room` (awkward-silence) is permanently ineligible because
# awkward-silence never has a cleanup owner to fill its required slot, so seeds 1-12
# all land on shared-space-repair (coffee-spill) or stuck-workflow (printer-jam).
CLEANUP_SEED = 1
NO_CLEANUP_SEED = 5


def test_init_scaffolds_a_registry_that_load_registry_accepts(tmp_path: Path) -> None:
    world = tmp_path / "tower"

    assert main(["init", "--world", str(world), "--seed", "42"]) == 0

    assert (world / "data" / "norms.toml").exists()
    registry = norms.load_registry(world)
    assert "clean-up-after-yourself" in registry.norms
    norm = registry.norms["clean-up-after-yourself"]
    assert norm.detection == "incident_cleanup_owner_missed"
    # integrity_drift intersects this against the starter character's declared_values;
    # without the overlap the drift criterion is vacuously satisfied by an empty list.
    assert "do competent work" in norm.related_values


def test_cleanup_incident_emits_a_norm_violation(tmp_path: Path, stub_narrator) -> None:
    world = tmp_path / "tower"
    tick_once(world, CLEANUP_SEED)

    incident_event = incident_by_id(world, "printer-jam")
    violations = incident_event["norm_violations"]
    assert [violation["norm_id"] for violation in violations] == ["clean-up-after-yourself"]
    assert violations[0]["evidence"]["cleanup_owner"] == "jordan-vale"
    assert violations[0]["evidence"]["resolved"] is False
    assert incident_event["norm_tags"]


def test_incident_without_cleanup_emits_no_violation(tmp_path: Path, stub_narrator) -> None:
    world = tmp_path / "tower"
    tick_once(world, NO_CLEANUP_SEED)

    incident_event = incident_by_id(world, "awkward-silence")
    assert incident_event["incident"]["cleanup_owner"] is None
    assert incident_event["norm_violations"] == []
    assert incident_event["norm_tags"] == []


def test_authored_incident_tags_survive_beside_detected_tags(
    tmp_path: Path, stub_narrator
) -> None:
    world = tmp_path / "tower"
    tick_once(world, CLEANUP_SEED)

    incident_event = incident_by_id(world, "printer-jam")
    # Nested: authored vocabulary for the incident type. Top-level: what the engine
    # detected this tick. Two different things — neither may absorb the other.
    assert incident_event["incident"]["norm_tags"] == ["duty", "patience"]
    assert incident_event["norm_tags"] == ["care", "shared-space"]


def test_scene_event_records_integrity_drift(tmp_path: Path, stub_narrator) -> None:
    world = tmp_path / "tower"
    tick_once(world, CLEANUP_SEED)

    scene_event = events_of(world, "scene")[0]
    drift = scene_event["integrity_drift"]
    assert drift, "a violated norm the character declared a value for must produce drift"
    assert drift[0]["character_id"] == "jordan-vale"
    assert drift[0]["norm_id"] == "clean-up-after-yourself"
    assert drift[0]["declared_values"] == ["do competent work"]


def test_scene_drift_covers_every_fired_incident_not_only_the_spotlight(
    tmp_path: Path, stub_narrator
) -> None:
    """The spotlight draw narrows the scene, never the character-integrity ledger."""
    world = tmp_path / "tower"
    tick_once(world, NO_CLEANUP_SEED)

    scene_event = events_of(world, "scene")[0]
    # The spotlight landed on one violating incident (coffee-spill)...
    assert scene_event["storylet_id"] == "shared-space-repair"
    # ...but the other fired, violating incident (printer-jam) was left uncleaned by
    # the same character, so its violation must still reach the drift ledger too, not
    # only the spotlighted incident's.
    violating = sorted(
        event["incident"]["id"]
        for event in events_of(world, "incident")
        if event["norm_violations"]
    )
    assert violating == ["coffee-spill", "printer-jam"]
    drift = scene_event["integrity_drift"]
    assert len(drift) == len(violating)
    assert [entry["norm_id"] for entry in drift] == ["clean-up-after-yourself"] * len(violating)
    assert {entry["character_id"] for entry in drift} == {"jordan-vale"}


def test_tick_without_a_registry_omits_the_norm_keys(tmp_path: Path, stub_narrator) -> None:
    """A world initialized before this change has no data/norms.toml and still ticks."""
    world = tmp_path / "tower"
    assert main(["init", "--world", str(world), "--seed", str(CLEANUP_SEED)]) == 0
    (world / "data" / "norms.toml").unlink()

    assert main(["tick", "--world", str(world)]) == 0

    for incident_event in events_of(world, "incident"):
        assert "norm_tags" not in incident_event
        assert "norm_violations" not in incident_event
    assert "integrity_drift" not in events_of(world, "scene")[0]
    # The tick still completed: state advanced and the chronicle was written.
    assert json.loads((world / "state" / "tower.json").read_text())["day"] == 1
    assert (world / "chronicles" / "day-0001.md").exists()
