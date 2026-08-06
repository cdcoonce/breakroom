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


# Seed 1 selects printer-jam on day 1 (needs_cleanup); seed 9 selects awkward-silence
# (no mess to clean). Both branches of the detector are reachable on a single tick.
CLEANUP_SEED = 1
NO_CLEANUP_SEED = 9


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

    incident_event = events_of(world, "incident")[0]
    assert incident_event["incident"]["id"] == "printer-jam"
    violations = incident_event["norm_violations"]
    assert [violation["norm_id"] for violation in violations] == ["clean-up-after-yourself"]
    assert violations[0]["evidence"]["cleanup_owner"] == "jordan-vale"
    assert violations[0]["evidence"]["resolved"] is False
    assert incident_event["norm_tags"]


def test_incident_without_cleanup_emits_no_violation(tmp_path: Path, stub_narrator) -> None:
    world = tmp_path / "tower"
    tick_once(world, NO_CLEANUP_SEED)

    incident_event = events_of(world, "incident")[0]
    assert incident_event["incident"]["id"] == "awkward-silence"
    assert incident_event["incident"]["cleanup_owner"] is None
    assert incident_event["norm_violations"] == []
    assert incident_event["norm_tags"] == []


def test_authored_incident_tags_survive_beside_detected_tags(
    tmp_path: Path, stub_narrator
) -> None:
    world = tmp_path / "tower"
    tick_once(world, CLEANUP_SEED)

    incident_event = events_of(world, "incident")[0]
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


def test_scene_event_has_no_drift_without_a_violation(tmp_path: Path, stub_narrator) -> None:
    world = tmp_path / "tower"
    tick_once(world, NO_CLEANUP_SEED)

    assert events_of(world, "scene")[0]["integrity_drift"] == []


def test_tick_without_a_registry_omits_the_norm_keys(tmp_path: Path, stub_narrator) -> None:
    """A world initialized before this change has no data/norms.toml and still ticks."""
    world = tmp_path / "tower"
    assert main(["init", "--world", str(world), "--seed", str(CLEANUP_SEED)]) == 0
    (world / "data" / "norms.toml").unlink()

    assert main(["tick", "--world", str(world)]) == 0

    incident_event = events_of(world, "incident")[0]
    assert "norm_tags" not in incident_event
    assert "norm_violations" not in incident_event
    assert "integrity_drift" not in events_of(world, "scene")[0]
    # The tick still completed: state advanced and the chronicle was written.
    assert json.loads((world / "state" / "tower.json").read_text())["day"] == 1
    assert (world / "chronicles" / "day-0001.md").exists()


def test_tick_does_not_mutate_the_shared_incident_table(tmp_path: Path, stub_narrator) -> None:
    """The widened payload must be a copy — INCIDENTS is module-level shared state."""
    from breakroom.tick import INCIDENTS

    world = tmp_path / "tower"
    tick_once(world, CLEANUP_SEED)

    for entry in INCIDENTS:
        assert "cleanup_owner" not in entry
        assert "resolved" not in entry
