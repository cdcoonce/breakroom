from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from breakroom.resolution.rng import RngStream, RollLog
from breakroom.secrets import (
    REVEAL_THRESHOLD,
    Secret,
    ValidationError,
    advance_exposure,
    maybe_reveal,
    read_secret,
    seal_secret,
)

CONTENT = "Jordan is quietly job-hunting."

NORMS_TOML = """\
[[norms]]
id = "client-confidentiality"
scope = "tower_policy"
description = "Client secrets stay with the authorized audience."
severity = "major"
detection = "secret_shared_with_unauthorized_audience"
tags = ["confidentiality"]
related_values = ["discretion"]
"""


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _new_world(tmp_path: Path, name: str = "tower") -> Path:
    world = tmp_path / name
    world.mkdir()
    (world / "events.jsonl").write_text("", encoding="utf-8")
    return world


def _store_path(world: Path) -> Path:
    return world / ".secrets" / world.name / "secrets.json"


def _seal(world: Path, **overrides: object) -> Secret:
    kwargs: dict = {
        "id": "affair-1",
        "holder": "jordan-vale",
        "content": CONTENT,
        "is_true": True,
    }
    kwargs.update(overrides)
    return seal_secret(world, **kwargs)


def _read_events(world: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (world / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_seal_secret_writes_sealed_store_and_returns_secret(tmp_path: Path) -> None:
    world = _new_world(tmp_path)

    secret = _seal(world)

    assert secret.state == "sealed"
    assert secret.revealed_by is None
    assert secret.knowers == ["jordan-vale"]
    assert secret.exposure_risk == 0.0

    stored = json.loads(_store_path(world).read_text(encoding="utf-8"))
    assert stored["affair-1"]["content"] == CONTENT


def test_seal_secret_clamps_risk_and_honors_explicit_knowers(tmp_path: Path) -> None:
    world = _new_world(tmp_path)

    secret = _seal(world, exposure_risk=1.5, knowers=["jordan-vale", "alex-chen"])

    assert secret.exposure_risk == 1.0
    assert secret.knowers == ["jordan-vale", "alex-chen"]


def test_seal_secret_rejects_duplicate_id(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    _seal(world)

    with pytest.raises(ValidationError, match="already sealed"):
        _seal(world)


def test_malformed_store_raises_validation_error(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    store = _store_path(world)
    store.parent.mkdir(parents=True)
    store.write_text("not json", encoding="utf-8")

    with pytest.raises(ValidationError, match="invalid JSON"):
        read_secret(world, "affair-1")


def test_store_record_missing_field_raises_validation_error(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    store = _store_path(world)
    store.parent.mkdir(parents=True)
    store.write_text(
        json.dumps({"affair-1": {"id": "affair-1", "holder": "jordan-vale"}}),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="missing content"):
        read_secret(world, "affair-1")


def test_store_record_with_wrong_state_raises_validation_error(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    _seal(world)
    store = _store_path(world)
    record = json.loads(store.read_text(encoding="utf-8"))
    record["affair-1"]["state"] = "burned"
    store.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValidationError, match="invalid state"):
        read_secret(world, "affair-1")


def test_read_secret_redacts_content_while_sealed(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    _seal(world)

    public = read_secret(world, "affair-1")

    assert "content" not in public
    assert public["state"] == "sealed"
    assert public["holder"] == "jordan-vale"


def test_advance_exposure_only_moves_risk_when_deltas_are_supplied(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    secret = _seal(world, exposure_risk=0.25)

    unchanged = advance_exposure(world, secret, seed=42, tick=1, deltas=[])

    assert unchanged.exposure_risk == 0.25

    advanced = advance_exposure(world, secret, seed=42, tick=1, deltas=[0.3])

    assert advanced.exposure_risk > 0.25


def test_advance_exposure_clamps_to_unit_range(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    secret = _seal(world)

    secret = advance_exposure(world, secret, seed=42, tick=1, deltas=[1.0] * 8)
    assert secret.exposure_risk == 1.0

    secret = advance_exposure(world, secret, seed=42, tick=2, deltas=[-1.0] * 8)
    assert secret.exposure_risk == 0.0


def test_advance_exposure_is_reproducible_under_seed_and_tick(tmp_path: Path) -> None:
    deltas = [0.3, 0.2, 0.4]
    runs = []
    for attempt in range(2):
        world = _new_world(tmp_path, f"tower-{attempt}")
        secret = _seal(world)
        secret = advance_exposure(world, secret, seed=7, tick=3, deltas=deltas)
        runs.append(secret.exposure_risk)

    assert runs[0] == runs[1]


def test_advance_exposure_draws_are_bound_to_the_exposure_stream(tmp_path: Path) -> None:
    deltas = [0.3, 0.2, 0.4]
    risks = {}
    for label, seed, tick in (("base", 7, 3), ("other_tick", 7, 4), ("other_seed", 8, 3)):
        world = _new_world(tmp_path, f"tower-{label}")
        secret = _seal(world)
        risks[label] = advance_exposure(
            world, secret, seed=seed, tick=tick, deltas=deltas
        ).exposure_risk

    assert risks["base"] != risks["other_tick"]
    assert risks["base"] != risks["other_seed"]


def test_advance_exposure_persists_to_the_sealed_store(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    secret = _seal(world)

    advanced = advance_exposure(world, secret, seed=42, tick=1, deltas=[0.6])

    assert read_secret(world, "affair-1")["exposure_risk"] == advanced.exposure_risk


def test_advance_exposure_rejects_unknown_secret(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    secret = _seal(world)
    _store_path(world).write_text(json.dumps({}), encoding="utf-8")

    with pytest.raises(ValidationError, match="unknown secret"):
        advance_exposure(world, secret, seed=42, tick=1, deltas=[0.1])


def test_advance_exposure_preserves_a_reveal_made_through_a_stale_handle(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    stale = _seal(world, exposure_risk=1.0)
    second_handle = Secret(**stale.to_record())

    revealed = maybe_reveal(
        world, second_handle, day=1, rng=RngStream(seed=1, stream="exposure", tick=1)
    )
    assert revealed.state == "observable"

    advanced = advance_exposure(world, stale, seed=1, tick=2, deltas=[0.1])

    assert advanced.state == "observable"
    assert advanced.revealed_by == revealed.revealed_by
    assert advanced.knowers == revealed.knowers

    stored = read_secret(world, "affair-1")
    assert stored["state"] == "observable"
    assert stored["revealed_by"] == revealed.revealed_by


def test_maybe_reveal_takes_no_draw_below_threshold(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    secret = _seal(world, exposure_risk=REVEAL_THRESHOLD - 0.01)

    log = RollLog()
    result = maybe_reveal(
        world, secret, day=1, rng=RngStream(seed=1, stream="exposure", tick=1, log=log)
    )

    assert result.state == "sealed"
    assert log.records == []
    assert _read_events(world) == []


def test_maybe_reveal_transitions_and_emits_secret_reveal_event(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    secret = _seal(world)
    secret = advance_exposure(world, secret, seed=42, tick=4, deltas=[1.0] * 8)
    assert secret.exposure_risk == 1.0

    revealed = maybe_reveal(
        world,
        secret,
        day=3,
        rng=RngStream(seed=42, stream="exposure", tick=4),
        observed_by=["alex-chen", "jordan-vale"],
    )

    assert revealed.state == "observable"
    assert revealed.knowers == ["jordan-vale", "alex-chen"]

    events = _read_events(world)
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "secret_reveal"
    assert event["day"] == 3
    assert event["secret_id"] == "affair-1"
    assert event["knowers"] == ["jordan-vale", "alex-chen"]
    assert event["provenance"]["trigger"] == "exposure_threshold"
    assert event["provenance"]["exposure_risk"] == 1.0
    assert event["provenance"]["content"] == CONTENT

    assert revealed.revealed_by == {
        "type": "secret_reveal",
        "sequence": event["sequence"],
        "day": 3,
    }

    stored = read_secret(world, "affair-1")
    assert stored["state"] == "observable"
    assert stored["content"] == CONTENT
    assert stored["knowers"] == ["jordan-vale", "alex-chen"]


def test_maybe_reveal_is_idempotent_once_observable(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    secret = _seal(world)
    secret = advance_exposure(world, secret, seed=42, tick=4, deltas=[1.0] * 8)
    revealed = maybe_reveal(world, secret, day=3, rng=RngStream(seed=42, stream="exposure", tick=4))

    again = maybe_reveal(
        world, revealed, day=4, rng=RngStream(seed=42, stream="exposure", tick=5)
    )

    assert again == revealed
    assert len(_read_events(world)) == 1


def test_maybe_reveal_preserves_exposure_risk_advanced_through_a_stale_handle(
    tmp_path: Path,
) -> None:
    world = _new_world(tmp_path)
    secret = _seal(world)
    first_handle = advance_exposure(world, secret, seed=1, tick=1, deltas=[1.0] * 4)
    assert REVEAL_THRESHOLD <= first_handle.exposure_risk < 1.0

    second_handle = advance_exposure(world, first_handle, seed=1, tick=2, deltas=[1.0] * 4)
    assert second_handle.exposure_risk > first_handle.exposure_risk

    revealed = maybe_reveal(
        world, first_handle, day=1, rng=RngStream(seed=1, stream="exposure", tick=1)
    )

    assert revealed.exposure_risk == second_handle.exposure_risk
    stored = read_secret(world, "affair-1")
    assert stored["exposure_risk"] == second_handle.exposure_risk


def test_reveal_outcome_is_reproducible_under_seed(tmp_path: Path) -> None:
    outcomes = []
    for attempt in range(2):
        world = _new_world(tmp_path, f"tower-{attempt}")
        secret = _seal(world)
        for tick in range(1, 4):
            secret = advance_exposure(world, secret, seed=99, tick=tick, deltas=[0.5, 0.5])
            secret = maybe_reveal(
                world, secret, day=tick, rng=RngStream(seed=99, stream="exposure", tick=tick)
            )
        outcomes.append((secret.state, secret.exposure_risk, len(_read_events(world))))

    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0] == "observable"


def test_reveal_tags_through_norms_when_a_registry_is_present(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    (world / "data").mkdir()
    (world / "data" / "norms.toml").write_text(NORMS_TOML, encoding="utf-8")
    secret = _seal(world)
    secret = advance_exposure(world, secret, seed=42, tick=4, deltas=[1.0] * 8)

    maybe_reveal(world, secret, day=1, rng=RngStream(seed=42, stream="exposure", tick=4))

    provenance = _read_events(world)[0]["provenance"]
    assert provenance["norm_tags"] == []
    assert provenance["norm_violations"] == []


def test_reveal_skips_norm_tagging_without_a_registry(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    secret = _seal(world)
    secret = advance_exposure(world, secret, seed=42, tick=4, deltas=[1.0] * 8)

    maybe_reveal(world, secret, day=1, rng=RngStream(seed=42, stream="exposure", tick=4))

    provenance = _read_events(world)[0]["provenance"]
    assert "norm_tags" not in provenance


def test_secret_content_is_absent_from_tracked_files_until_reveal(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    (world / ".gitignore").write_text(".secrets/\n", encoding="utf-8")
    _git("init", cwd=world)
    _git("config", "user.email", "test@example.com", cwd=world)
    _git("config", "user.name", "test", cwd=world)

    secret = _seal(world)
    for tick in range(1, 4):
        secret = advance_exposure(world, secret, seed=42, tick=tick, deltas=[0.1])

    _git("add", "-A", cwd=world)
    before = _tracked_files(world)
    assert before, "fixture should have written at least one tracked file"
    assert [path for path in before if CONTENT in (world / path).read_text(encoding="utf-8")] == []

    secret = advance_exposure(world, secret, seed=42, tick=4, deltas=[1.0] * 8)
    assert secret.exposure_risk == 1.0
    secret = maybe_reveal(world, secret, day=4, rng=RngStream(seed=42, stream="exposure", tick=4))
    assert secret.state == "observable"

    _git("add", "-A", cwd=world)
    after = _tracked_files(world)
    assert [path for path in after if CONTENT in (world / path).read_text(encoding="utf-8")]


def _tracked_files(world: Path) -> list[str]:
    status = _git("status", "--porcelain", cwd=world)
    paths = [line[3:] for line in status.splitlines() if line.strip()]
    return [path for path in paths if (world / path).is_file()]
