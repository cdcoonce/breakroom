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
from breakroom.worldstate import ValidationError as WorldstateValidationError

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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", ["unhashable-id"]),
        ("id", 17),
        ("holder", 17),
        ("content", 17),
        ("is_true", 1),
        ("knowers", ("alex-chen",)),
        ("knowers", 17),
    ],
)
def test_seal_secret_rejects_invalid_input_without_changing_store(
    tmp_path: Path, field: str, value: object
) -> None:
    world = _new_world(tmp_path)
    _seal(world)
    before_public = read_secret(world, "affair-1")
    assert before_public["id"] == "affair-1"
    store = _store_path(world)
    original_bytes = store.read_bytes()
    kwargs = {"id": "new-secret", field: value}

    with pytest.raises(WorldstateValidationError):
        _seal(world, **kwargs)

    assert store.read_bytes() == original_bytes
    assert read_secret(world, "affair-1") == before_public


def test_invalid_seal_input_precedes_valid_duplicate_rejection(
    tmp_path: Path,
) -> None:
    world = _new_world(tmp_path)
    _seal(world)
    store = _store_path(world)
    original_bytes = store.read_bytes()

    with pytest.raises(WorldstateValidationError, match="knowers must be a list"):
        _seal(world, knowers=("alex-chen",))

    assert store.read_bytes() == original_bytes


def test_healthy_secret_operations_preserve_unrelated_malformed_records(
    tmp_path: Path,
) -> None:
    world = _new_world(tmp_path)
    healthy = _seal(world, exposure_risk=1.0)
    public_before = read_secret(world, "affair-1")
    malformed = {"id": "broken", "unexpected": ["leave", "alone"]}
    store_path = _store_path(world)
    store = json.loads(store_path.read_text(encoding="utf-8"))
    store["broken"] = malformed
    store_path.write_text(json.dumps(store), encoding="utf-8")

    assert read_secret(world, "affair-1") == public_before

    advanced = advance_exposure(world, healthy, seed=42, tick=1, deltas=[0.1])
    revealed = maybe_reveal(
        world,
        advanced,
        day=1,
        rng=RngStream(seed=42, stream="exposure", tick=2),
    )
    assert revealed.state == "observable"

    seal_secret(
        world,
        id="new-secret",
        holder="alex-chen",
        content="A separate fact.",
        is_true=False,
    )

    assert json.loads(store_path.read_text(encoding="utf-8"))["broken"] == malformed


def test_seal_secret_validates_malformed_duplicate_record_before_rejecting_duplicate(
    tmp_path: Path,
) -> None:
    world = _new_world(tmp_path)
    store_path = _store_path(world)
    store_path.parent.mkdir(parents=True)
    malformed = {"id": "affair-1", "holder": "jordan-vale"}
    store_path.write_text(json.dumps({"affair-1": malformed}), encoding="utf-8")

    with pytest.raises(ValidationError, match="missing content"):
        _seal(world)

    assert json.loads(store_path.read_text(encoding="utf-8"))["affair-1"] == malformed


@pytest.mark.parametrize("operation", ["advance", "reveal"])
def test_secret_operations_reject_malformed_accessed_record(
    tmp_path: Path, operation: str
) -> None:
    world = _new_world(tmp_path)
    secret = _seal(world, exposure_risk=1.0)
    store_path = _store_path(world)
    store = json.loads(store_path.read_text(encoding="utf-8"))
    del store["affair-1"]["content"]
    store_path.write_text(json.dumps(store), encoding="utf-8")
    original_bytes = store_path.read_bytes()

    with pytest.raises(ValidationError, match="missing content"):
        if operation == "advance":
            advance_exposure(world, secret, seed=1, tick=1, deltas=[0.2])
        else:
            maybe_reveal(
                world,
                secret,
                day=1,
                rng=RngStream(seed=1, stream="exposure", tick=1),
            )

    assert store_path.read_bytes() == original_bytes


def test_malformed_store_raises_validation_error(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    store = _store_path(world)
    store.parent.mkdir(parents=True)
    store.write_text("not json", encoding="utf-8")

    with pytest.raises(ValidationError, match="invalid JSON"):
        read_secret(world, "affair-1")


def test_non_object_store_raises_validation_error(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    store = _store_path(world)
    store.parent.mkdir(parents=True)
    store.write_text("[]", encoding="utf-8")

    with pytest.raises(ValidationError, match="store must be a JSON object"):
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


def test_read_secret_rejects_a_null_record_as_malformed(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    store = _store_path(world)
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps({"affair-1": None}), encoding="utf-8")

    with pytest.raises(ValidationError, match="record for affair-1 must be an object"):
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


def test_store_record_with_id_mismatch_raises_validation_error(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    _seal(world)
    store = _store_path(world)
    record = json.loads(store.read_text(encoding="utf-8"))
    record["affair-1"]["id"] = "affair-2"
    store.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValidationError, match="does not match store key"):
        read_secret(world, "affair-1")


def test_read_secret_redacts_content_while_sealed(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    _seal(world)

    public = read_secret(world, "affair-1")

    assert "content" not in public
    assert public["state"] == "sealed"
    assert public["holder"] == "jordan-vale"


@pytest.mark.parametrize("truth", [True, False], ids=["true-secret", "false-secret"])
def test_public_views_hide_truth_until_reveal_without_changing_private_records(
    tmp_path: Path, truth: bool
) -> None:
    world = _new_world(tmp_path)
    secret = _seal(world, is_true=truth, exposure_risk=1.0, knowers=["jordan-vale", "alex-chen"])
    metadata = {
        "id": "affair-1",
        "holder": "jordan-vale",
        "exposure_risk": 1.0,
        "knowers": ["jordan-vale", "alex-chen"],
        "state": "sealed",
        "revealed_by": None,
    }
    private_record = {**metadata, "content": CONTENT, "is_true": truth}
    store = _store_path(world)
    sealed_bytes = store.read_bytes()

    # Evaluate both entry points even if the first view violates the boundary.
    sealed_views = (secret.public_view(), read_secret(world, secret.id))

    for view in sealed_views:
        assert "content" not in view
        assert "is_true" not in view
        assert view == metadata
    assert secret.to_record() == private_record
    assert secret.to_record()["is_true"] is truth
    assert json.loads(store.read_text())[secret.id] == private_record
    assert json.loads(store.read_text())[secret.id]["is_true"] is truth
    assert store.read_bytes() == sealed_bytes

    observable = maybe_reveal(
        world, secret, day=1, rng=RngStream(seed=42, stream="exposure", tick=1)
    )
    assert observable.state == "observable"
    observable_record = {
        **private_record,
        "state": "observable",
        "revealed_by": {"type": "secret_reveal", "sequence": 1, "day": 1},
    }
    observable_bytes = store.read_bytes()

    observable_views = (observable.public_view(), read_secret(world, observable.id))

    for view in observable_views:
        assert view == observable_record
        assert view["content"] == CONTENT
        assert view["is_true"] is truth
    assert observable.to_record() == observable_record
    assert observable.to_record()["is_true"] is truth
    assert json.loads(store.read_text())[observable.id] == observable_record
    assert json.loads(store.read_text())[observable.id]["is_true"] is truth
    assert store.read_bytes() == observable_bytes


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


def test_advance_exposure_draws_are_bound_to_the_secret_id(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    first = _seal(world, id="affair-1", exposure_risk=0.0)
    second = _seal(world, id="affair-2", exposure_risk=0.0)

    first_risk = advance_exposure(
        world, first, seed=7, tick=3, deltas=[0.3]
    ).exposure_risk
    second_risk = advance_exposure(
        world, second, seed=7, tick=3, deltas=[0.3]
    ).exposure_risk

    assert 0.0 < first_risk < 0.3
    assert 0.0 < second_risk < 0.3
    assert first_risk != second_risk

    repeat_world = _new_world(tmp_path, "repeat-tower")
    repeat_start = _seal(repeat_world, id="affair-1", exposure_risk=0.0)
    repeated_risk = advance_exposure(
        repeat_world, repeat_start, seed=7, tick=3, deltas=[0.3]
    ).exposure_risk

    assert repeated_risk == first_risk


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


def test_maybe_reveal_returns_early_without_loading_store(tmp_path: Path) -> None:
    from dataclasses import replace

    world = _new_world(tmp_path)
    secret = replace(_seal(world, exposure_risk=1.0), state="observable")
    _store_path(world).write_text("not json", encoding="utf-8")

    result = maybe_reveal(
        world, secret, day=1, rng=RngStream(seed=1, stream="exposure", tick=1)
    )

    assert result == secret


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


def test_maybe_reveal_uses_persisted_risk_for_threshold_and_provenance(
    tmp_path: Path,
) -> None:
    world = _new_world(tmp_path)
    stale = _seal(world, exposure_risk=0.0)
    advanced = advance_exposure(world, stale, seed=1, tick=1, deltas=[1.0] * 4)
    assert advanced.exposure_risk > REVEAL_THRESHOLD

    log = RollLog()
    revealed = maybe_reveal(
        world,
        stale,
        day=1,
        rng=RngStream(seed=1, stream="exposure", tick=1, log=log),
    )

    assert revealed.state == "observable"
    assert log.records[0]["purpose"] == "reveal"
    assert log.records[0]["primitive"] == "bernoulli"
    event = _read_events(world)[0]
    assert event["provenance"]["exposure_risk"] == advanced.exposure_risk
    assert revealed.exposure_risk == advanced.exposure_risk
    assert read_secret(world, stale.id)["exposure_risk"] == advanced.exposure_risk


def test_maybe_reveal_uses_persisted_risk_for_draw_probability(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    stale = _seal(world, exposure_risk=0.8)
    store_path = _store_path(world)
    store = json.loads(store_path.read_text(encoding="utf-8"))
    store[stale.id]["exposure_risk"] = 0.9
    store_path.write_text(json.dumps(store), encoding="utf-8")

    log = RollLog()
    revealed = maybe_reveal(
        world,
        stale,
        day=1,
        rng=RngStream(seed=3, stream="exposure", tick=19, log=log),
    )

    assert log.records == [
        {
            "stream": "exposure",
            "tick": 19,
            "purpose": "reveal",
            "primitive": "bernoulli",
            "result": True,
        }
    ]
    assert revealed.state == "observable"
    assert revealed.exposure_risk == 0.9
    assert _read_events(world)[0]["provenance"]["exposure_risk"] == 0.9


def test_maybe_reveal_ignores_a_stale_sealed_handle_after_reveal(tmp_path: Path) -> None:
    world = _new_world(tmp_path)
    stale = _seal(world, exposure_risk=1.0)
    revealed = maybe_reveal(
        world, stale, day=1, rng=RngStream(seed=1, stream="exposure", tick=1)
    )
    events = _read_events(world)
    assert revealed.state == "observable"

    result = maybe_reveal(
        world, stale, day=2, rng=RngStream(seed=1, stream="exposure", tick=2)
    )

    assert result is stale
    assert _read_events(world) == events


def test_maybe_reveal_preserves_exposure_risk_advanced_through_a_stale_handle(
    tmp_path: Path,
) -> None:
    world = _new_world(tmp_path)
    secret = _seal(world)
    first_handle = advance_exposure(world, secret, seed=6, tick=1, deltas=[1.0])
    assert REVEAL_THRESHOLD <= first_handle.exposure_risk < 1.0

    second_handle = advance_exposure(world, first_handle, seed=6, tick=2, deltas=[1.0])
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


@pytest.mark.parametrize(
    "registry_contents",
    [None, NORMS_TOML, "norms = ["],
    ids=["no-registry", "registry", "malformed-registry"],
)
def test_reveal_provenance_has_stable_norm_arrays_with_or_without_registry(
    tmp_path: Path, registry_contents: str | None
) -> None:
    world = _new_world(tmp_path)
    if registry_contents is not None:
        (world / "data").mkdir()
        (world / "data" / "norms.toml").write_text(registry_contents, encoding="utf-8")
    secret = _seal(world, exposure_risk=1.0)
    log = RollLog()

    revealed = maybe_reveal(
        world,
        secret,
        day=1,
        rng=RngStream(seed=42, stream="exposure", tick=4, log=log),
        observed_by=["alex-chen"],
    )

    expected_provenance = {
        "trigger": "exposure_threshold",
        "exposure_risk": 1.0,
        "tick": 4,
        "stream": "exposure",
        "holder": "jordan-vale",
        "is_true": True,
        "content": CONTENT,
        "norm_tags": [],
        "norm_violations": [],
    }
    expected_knowers = ["jordan-vale", "alex-chen"]
    events = _read_events(world)

    assert revealed.state == "observable"
    assert revealed.exposure_risk == 1.0
    assert revealed.knowers == expected_knowers
    assert revealed.revealed_by == {"type": "secret_reveal", "sequence": 1, "day": 1}
    assert events == [
        {
            "sequence": 1,
            "type": "secret_reveal",
            "day": 1,
            "secret_id": "affair-1",
            "knowers": expected_knowers,
            "provenance": expected_provenance,
        }
    ]
    assert read_secret(world, secret.id)["state"] == "observable"
    assert log.records == [
        {
            "stream": "exposure",
            "tick": 4,
            "purpose": "reveal",
            "primitive": "bernoulli",
            "result": True,
        }
    ]


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
