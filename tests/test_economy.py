from __future__ import annotations

import ast
import hashlib
import importlib
import json
import math
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from breakroom.init import init_world
from breakroom.worldstate import ValidationError, apply_event

REPO_ROOT = Path(__file__).resolve().parents[1]


def economy_api():
    return importlib.import_module("breakroom.economy")


def _canonical_digest(rulebook: dict) -> str:
    semantic = json.dumps(
        rulebook,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(semantic).hexdigest()


def _incident(delta=-2) -> dict:
    return {"type": "incident", "day": 1, "incident": {"id": "spill", "morale_delta": delta}}


def _dial_delta(dials: dict) -> dict:
    return {"type": "dial_delta", "day": 1, "dials": dials}


def _v1(dials: dict, **overrides) -> dict:
    result = {"version": 1, "rulebook_sha256": "a" * 64, "dials": dials}
    result.update(overrides)
    return result


def test_bundled_rulebook_hash_is_canonical_semantic_json(tmp_path: Path, monkeypatch) -> None:
    api = economy_api()
    world = tmp_path / "world-without-override"
    world.mkdir()
    decoy = tmp_path / "cwd-decoy" / "data"
    decoy.mkdir(parents=True)
    (decoy / "economy.toml").write_text(
        '[events.incident]\ndial = "reputation"\namount_path = "incident.morale_delta"\n'
        '[events.dial_delta]\ndials_path = "dials"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(decoy.parent)

    rulebook = api.load_rulebook(world)
    receipt = api.resolve_dial_movement(_incident(), rulebook)

    assert receipt["dial_movement"] == {
        "version": 1,
        "rulebook_sha256": _canonical_digest(rulebook),
        "dials": {"morale": -2},
    }


def test_world_override_selects_future_mapping_but_not_frozen_receipts(tmp_path: Path) -> None:
    api = economy_api()
    world = tmp_path / "world"
    init_world(world, seed=42)
    override = world / "data" / "economy.toml"
    assert override.is_file()
    assert api.load_rulebook(world)["events"]["incident"]["dial"] == "morale"
    override.write_text(
        '[events.incident]\ndial = "reputation"\namount_path = "incident.morale_delta"\n'
        '[events.dial_delta]\ndials_path = "dials"\n',
        encoding="utf-8",
    )
    source = _incident(-3)
    original = json.loads(json.dumps(source))
    rulebook = api.load_rulebook(world)
    frozen = api.resolve_dial_movement(source, rulebook)
    fallback_world = tmp_path / "no-override"
    fallback_world.mkdir()
    later_rulebook = api.load_rulebook(fallback_world)

    assert frozen["dial_movement"]["dials"] == {"reputation": -3}
    assert frozen["dial_movement"]["rulebook_sha256"] == _canonical_digest(rulebook)
    assert frozen["dial_movement"]["rulebook_sha256"] != _canonical_digest(later_rulebook)
    assert source == original
    assert api.resolve_dial_movement(source, later_rulebook)["dial_movement"]["dials"] == {
        "morale": -3
    }


@pytest.mark.parametrize(
    "invalid_toml",
    [
        "[events.incident\n",
        '[events.incident]\ndial = "unknown"\namount_path = "incident.morale_delta"\n',
        '[events.incident]\ndial = "morale"\namount_path = "incident.morale_delta"\n'
        '[events.dial_delta]\ndials_path = "dials"\nextra = true\n',
        '[events.incident]\ndial = "morale"\namount_path = "incident.morale_delta"\n'
        '[events.dial_delta]\ndials_path = "dials"\n'
        "[unexpected]\nvalue = 1\n",
    ],
)
def test_present_invalid_rulebook_fails_closed_without_bundle_fallback(
    tmp_path: Path, invalid_toml: str
) -> None:
    api = economy_api()
    world = tmp_path / "world"
    (world / "data").mkdir(parents=True)
    (world / "data" / "economy.toml").write_text(invalid_toml, encoding="utf-8")

    with pytest.raises(ValidationError):
        api.load_rulebook(world)


def test_resolver_returns_independent_copies_and_validated_hashes(tmp_path: Path) -> None:
    api = economy_api()
    world = tmp_path / "world"
    init_world(world, seed=42)
    rulebook_a = api.load_rulebook(world)
    rulebook_b = json.loads(json.dumps(rulebook_a))
    rulebook_b["events"]["incident"]["dial"] = "reputation"
    event = _incident(-4)
    original = json.loads(json.dumps(event))

    first = api.resolve_dial_movement(event, rulebook_a)
    second = api.resolve_dial_movement(event, rulebook_b)

    assert event == original
    assert first is not event and second is not event and first is not second
    assert first["dial_movement"]["dials"] == {"morale": -4}
    assert second["dial_movement"]["dials"] == {"reputation": -4}
    assert first["dial_movement"]["rulebook_sha256"] == _canonical_digest(rulebook_a)
    assert second["dial_movement"]["rulebook_sha256"] == _canonical_digest(rulebook_b)


def test_resolver_uses_incident_default_zero_and_rejects_malformed_new_amounts(
    tmp_path: Path,
) -> None:
    api = economy_api()
    rulebook = api.load_rulebook(tmp_path)

    no_delta = api.resolve_dial_movement({"type": "incident", "incident": {"id": "x"}}, rulebook)
    assert no_delta["dial_movement"]["dials"] == {"morale": 0}

    for amount in (True, "2", float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            api.resolve_dial_movement(_incident(amount), rulebook)
    for payload in ({"budget": True}, {"budget": float("nan")}, {"custom": 1}):
        with pytest.raises(ValidationError):
            api.resolve_dial_movement(_dial_delta(payload), rulebook)


def test_new_move_clamps_dials_is_pure_and_keeps_finite_budget_unbounded(tmp_path: Path) -> None:
    api = economy_api()
    world = tmp_path / "world"
    init_world(world, seed=42)
    rulebook = api.load_rulebook(world)
    state = {"budget": 10, "morale": 99, "reputation": 1}
    event = _dial_delta({"budget": -20, "morale": 10, "reputation": -10})
    original_state = json.loads(json.dumps(state))
    original_event = json.loads(json.dumps(event))

    changed = api.move_dial(state, event, rulebook=rulebook)

    assert changed == {"budget": -10, "morale": 100, "reputation": 0}
    assert state == original_state and event == original_event
    huge = 10**1000
    large_state = {"budget": huge, "morale": 50, "reputation": 50}
    assert api.move_dial(large_state, _dial_delta({"budget": 1}))["budget"] == huge + 1


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), float("-inf")])
def test_new_versioned_movement_rejects_nonfinite_or_bool_amounts(tmp_path: Path, bad) -> None:
    api = economy_api()
    event = _dial_delta({"budget": bad})
    event["dial_movement"] = _v1({"budget": bad})
    with pytest.raises(ValidationError):
        api.move_dial({"budget": 0, "morale": 50, "reputation": 50}, event)


@pytest.mark.parametrize("current", [True, float("nan"), float("inf"), float("-inf")])
def test_new_movement_rejects_invalid_current_values(tmp_path: Path, current) -> None:
    api = economy_api()
    with pytest.raises(ValidationError):
        api.move_dial(
            {"budget": current, "morale": 50, "reputation": 50},
            _dial_delta({"budget": 1}),
        )


def test_new_movement_rejects_float_overflow_before_clamp(tmp_path: Path) -> None:
    api = economy_api()
    state = {"budget": 1.7e308, "morale": 50, "reputation": 50}
    with pytest.raises(ValidationError, match="finite"):
        api.move_dial(state, _dial_delta({"budget": 1.7e308}))
    with pytest.raises(ValidationError, match="finite"):
        api.move_dial(
            {"budget": 0, "morale": 1.7e308, "reputation": 50},
            _dial_delta({"morale": 1.7e308}),
        )
    event = _dial_delta({"budget": 10**1000})
    event["dial_movement"] = _v1({"budget": 10**1000})
    with pytest.raises(ValidationError, match="finite"):
        api.move_dial({"budget": 1.0, "morale": 50, "reputation": 50}, event)


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        "v1",
        {"version": True, "rulebook_sha256": "a" * 64, "dials": {"custom": 3}},
        {"version": 1.0, "rulebook_sha256": "a" * 64, "dials": {"budget": 1}},
        {"version": 2, "rulebook_sha256": "a" * 64, "dials": {"budget": 1}},
        {"version": 1, "rulebook_sha256": "A" * 64, "dials": {"budget": 1}},
        {"version": 1, "rulebook_sha256": "a" * 63, "dials": {"budget": 1}},
        {"version": 1, "rulebook_sha256": "a" * 64, "dials": {"budget": 1}, "extra": 1},
        {"version": 1, "rulebook_sha256": "a" * 64},
        {"version": 1, "rulebook_sha256": "a" * 64, "dials": {"custom": 1}},
        {"version": 1, "rulebook_sha256": "a" * 64, "dials": {"morale": False}},
        {"version": 1, "rulebook_sha256": "a" * 64, "dials": {"morale": float("nan")}},
    ],
)
def test_present_invalid_metadata_never_falls_back_to_legacy(metadata) -> None:
    api = economy_api()
    state = {"day": 0, "morale": 50, "reputation": 50, "budget": 0, "custom": 5}
    event = _dial_delta({"custom": 2})
    event["dial_movement"] = metadata

    with pytest.raises(ValidationError):
        api.move_dial(state, event)
    with pytest.raises(ValidationError):
        apply_event(state, event)


def test_valid_v1_replay_uses_frozen_dials_not_current_rulebook(tmp_path: Path) -> None:
    api = economy_api()
    world = tmp_path / "world"
    init_world(world, seed=42)
    rulebook = api.load_rulebook(world)
    frozen = api.resolve_dial_movement(_incident(-7), rulebook)
    # The source payload and current rulebook are deliberately changed after resolution.
    frozen["incident"]["morale_delta"] = 1000
    frozen["dial_movement"]["dials"] = {"reputation": 3}
    current = json.loads(json.dumps(rulebook))
    current["events"]["incident"]["dial"] = "budget"

    changed = apply_event({"day": 0, "budget": 1000, "morale": 50, "reputation": 50}, frozen)

    assert changed["budget"] == 1000
    assert changed["morale"] == 50
    assert changed["reputation"] == 53
    assert current["events"]["incident"]["dial"] == "budget"


def test_legacy_reducer_keeps_arbitrary_keys_and_nonfinite_arithmetic() -> None:
    api = economy_api()
    state = {"day": 0, "budget": 1, "morale": 50, "reputation": 50, "custom": 10}
    legacy = api.move_dial(
        state, _dial_delta({"custom": 2, "budget": float("inf")}), legacy_unclamped=True
    )
    assert legacy["custom"] == 12
    assert math.isinf(legacy["budget"])
    assert state["custom"] == 10 and state["budget"] == 1

    incident = apply_event(
        {"day": 0, "budget": 1, "morale": 50, "reputation": 50},
        {"type": "incident", "day": 1, "incident": {"morale_delta": float("nan")}},
    )
    assert math.isnan(incident["morale"])


def test_legacy_flag_rejects_rulebook_and_any_present_metadata(tmp_path: Path) -> None:
    api = economy_api()
    state = {"budget": 1, "morale": 50, "reputation": 50}
    raw = _incident()
    with pytest.raises(ValidationError):
        api.move_dial(state, raw, rulebook={})
    for metadata in ({"version": 1}, None):
        event = {**raw, "dial_movement": metadata}
        with pytest.raises(ValidationError):
            api.move_dial(state, event, legacy_unclamped=True)


def test_wheel_installs_bundled_rulebook_as_readable_package_resource(tmp_path: Path) -> None:
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_dir.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert "breakroom/data/economy.toml" in archive.namelist()

    install_dir = tmp_path / "installed"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(install_dir),
            str(wheel),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    env = dict(os.environ, PYTHONPATH=str(install_dir))
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import breakroom, hashlib; from importlib.resources import files; "
            "p = files('breakroom').joinpath('data/economy.toml'); "
            "assert p.is_file(); print(breakroom.__file__); "
            "print(hashlib.sha256(p.read_bytes()).hexdigest())",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    lines = probe.stdout.splitlines()
    assert str(install_dir) in lines[0]
    assert len(lines[1]) == 64


def test_only_economy_mutator_contains_dynamic_dial_state_writes() -> None:
    sources = [
        REPO_ROOT / "src" / "breakroom" / "worldstate.py",
        REPO_ROOT / "src" / "breakroom" / "tick.py",
        REPO_ROOT / "src" / "breakroom" / "init.py",
        REPO_ROOT / "src" / "breakroom" / "economy.py",
    ]
    forbidden = {"budget", "morale", "reputation"}
    violations = []
    for source in sources:
        if not source.exists():
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))

        class Visitor(ast.NodeVisitor):
            def __init__(self, source_name: str):
                self.source_name = source_name
                self.functions: list[str] = []

            def visit_FunctionDef(self, node):
                self.functions.append(node.name)
                self.generic_visit(node)
                self.functions.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Assign(self, node):
                self._check(node, node.targets)
                self.generic_visit(node)

            def visit_AnnAssign(self, node):
                self._check(node, [node.target])
                self.generic_visit(node)

            def visit_AugAssign(self, node):
                self._check(node, [node.target])
                self.generic_visit(node)

            def _check(self, node, targets):
                for target in targets:
                    if not isinstance(target, ast.Subscript):
                        continue
                    key = target.slice
                    is_dial = isinstance(key, ast.Constant) and key.value in forbidden
                    is_state_dynamic = (
                        isinstance(key, ast.Name)
                        and key.id == "dial"
                        and isinstance(target.value, ast.Name)
                        and target.value.id in {"state", "result", "next_state"}
                    )
                    is_mutator = self.source_name == "economy.py" and self.functions[-1:] == [
                        "move_dial"
                    ]
                    if (is_dial or is_state_dynamic) and not is_mutator:
                        violations.append(f"{self.source_name}:{node.lineno}")

        Visitor(source.name).visit(tree)

    assert violations == []
