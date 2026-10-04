from __future__ import annotations

import importlib
import json
import math
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zipfile import ZipFile

import pytest

from breakroom.worldstate import ValidationError


def thresholds_api():
    return importlib.import_module("breakroom.economy")


def _write_threshold(directory: Path, name: str, *, dial="morale", trip=10, rearm=20) -> None:
    directory.mkdir(parents=True, exist_ok=True)

    def toml_value(value: Any) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        return repr(value)

    (directory / f"{name}.toml").write_text(
        f"dial = {toml_value(dial)}\ntrip = {toml_value(trip)}\n"
        f"rearm = {toml_value(rearm)}\n",
        encoding="utf-8",
    )


def _registry(tmp_path: Path, entries: dict[str, tuple[str, int | float, int | float]]):
    directory = tmp_path / "world" / "data" / "thresholds"
    for name, (dial, trip, rearm) in entries.items():
        _write_threshold(directory, name, dial=dial, trip=trip, rearm=rearm)
    return thresholds_api().load_thresholds(tmp_path / "world")


def test_bundled_defaults_trip_and_order_without_cwd_lookup(tmp_path: Path, monkeypatch) -> None:
    api = thresholds_api()
    decoy = tmp_path / "cwd" / "data" / "thresholds"
    _write_threshold(decoy, "morale_crisis", dial="morale", trip=90, rearm=95)
    monkeypatch.chdir(decoy.parents[1])

    registry = api.load_thresholds(tmp_path / "world-without-thresholds")
    events, active = api.check_thresholds({"morale": 20, "budget": 250}, frozenset())

    assert dict(registry) == {
        "budget_crisis": api.ThresholdDefinition(dial="budget", trip=250, rearm=300),
        "morale_crisis": api.ThresholdDefinition(dial="morale", trip=20, rearm=30),
    }
    assert events == [
        {"type": "condition", "name": "budget_crisis", "active": True},
        {"type": "condition", "name": "morale_crisis", "active": True},
    ]
    assert active == frozenset({"budget_crisis", "morale_crisis"})
    assert isinstance(active, frozenset)


@pytest.mark.parametrize(
    ("dial", "trip", "rearm", "inside", "name"),
    [
        ("morale", 20, 30, 25, "morale_crisis"),
        ("budget", 250, 300, 275, "budget_crisis"),
    ],
)
def test_inclusive_trip_rearm_and_dead_band_hysteresis(
    dial: str, trip: int, rearm: int, inside: int, name: str
) -> None:
    api = thresholds_api()
    state = {"morale": 50, "budget": 400}
    state[dial] = trip + 1
    active = frozenset()

    events, active = api.check_thresholds(state, active)
    assert events == []
    assert active == frozenset()

    state[dial] = trip
    events, active = api.check_thresholds(state, active)
    assert events == [{"type": "condition", "name": name, "active": True}]
    assert active == frozenset({name})

    for value in (inside, trip, inside, rearm - 1):
        state[dial] = value
        events, next_active = api.check_thresholds(state, active)
        assert events == []
        assert next_active == active
        active = next_active

    state[dial] = rearm
    events, active = api.check_thresholds(state, active)
    assert events == [{"type": "condition", "name": name, "active": False}]
    assert active == frozenset()

    state[dial] = rearm - 1
    events, next_active = api.check_thresholds(state, active)
    assert events == []
    assert next_active == active


def test_explicit_world_registry_is_complete_and_loader_falls_back_only_when_absent(
    tmp_path: Path,
) -> None:
    api = thresholds_api()
    absent = api.load_thresholds(tmp_path / "no-overrides")
    assert set(absent) == {"morale_crisis", "budget_crisis"}

    registry = _registry(tmp_path, {"custom_alert": ("morale", 5, 9)})
    assert set(registry) == {"custom_alert"}
    assert api.check_thresholds({"morale": 5}, frozenset(), thresholds=registry) == (
        [{"type": "condition", "name": "custom_alert", "active": True}],
        frozenset({"custom_alert"}),
    )
    with pytest.raises(ValidationError, match="unknown|active"):
        api.check_thresholds({"morale": 5}, frozenset({"morale_crisis"}), thresholds=registry)


def test_threshold_engine_is_pure_and_preserves_frozen_active_when_unchanged(
    tmp_path: Path,
) -> None:
    api = thresholds_api()
    registry = _registry(tmp_path, {"custom_alert": ("morale", 5, 9)})
    state = {"morale": 7}
    original_state = state.copy()
    original_registry = dict(registry)
    active = frozenset({"custom_alert"})

    events, unchanged = api.check_thresholds(state, active, thresholds=registry)

    assert events == []
    assert unchanged == active
    assert isinstance(unchanged, frozenset)
    assert state == original_state
    assert dict(registry) == original_registry
    with pytest.raises((AttributeError, TypeError)):
        registry["mutated"] = api.ThresholdDefinition(dial="morale", trip=1, rearm=2)


@pytest.mark.parametrize(
    "contents",
    [
        "[broken\n",
        'dial = "unknown"\ntrip = 1\nrearm = 2\n',
        'dial = []\ntrip = 1\nrearm = 2\n',
        'dial = "morale"\ntrip = true\nrearm = 2\n',
        'dial = "morale"\ntrip = 1\nrearm = false\n',
        'dial = "morale"\ntrip = 2\nrearm = 2\n',
        'dial = "morale"\ntrip = 3\nrearm = 2\n',
        'dial = "morale"\ntrip = nan\nrearm = 2\n',
        'dial = "morale"\ntrip = 1\nrearm = inf\n',
        'dial = "morale"\ntrip = 1\nrearm = 2\nextra = true\n',
        'dial = "morale"\ntrip = 1\n',
    ],
)
def test_loader_rejects_invalid_threshold_file_schema(tmp_path: Path, contents: str) -> None:
    api = thresholds_api()
    directory = tmp_path / "world" / "data" / "thresholds"
    directory.mkdir(parents=True)
    (directory / "custom_alert.toml").write_text(contents, encoding="utf-8")

    with pytest.raises(ValidationError):
        api.load_thresholds(tmp_path / "world")


@pytest.mark.parametrize(
    "filename",
    [
        "Custom.toml",
        "custom.TOML",
        "_custom.toml",
        "1custom.toml",
        "éclair.toml",
        "custom-name.toml",
    ],
)
def test_loader_rejects_invalid_threshold_filename_ids(tmp_path: Path, filename: str) -> None:
    api = thresholds_api()
    directory = tmp_path / "world" / "data" / "thresholds"
    directory.mkdir(parents=True)
    (directory / filename).write_text('dial = "morale"\ntrip = 1\nrearm = 2\n', encoding="utf-8")

    with pytest.raises(ValidationError):
        api.load_thresholds(tmp_path / "world")


@pytest.mark.parametrize("child", ["readme.txt", "nested"])
def test_loader_rejects_unexpected_children_and_empty_present_registry(
    tmp_path: Path, child: str
) -> None:
    api = thresholds_api()
    directory = tmp_path / "world" / "data" / "thresholds"
    directory.mkdir(parents=True)
    if child == "nested":
        (directory / child).mkdir()
    else:
        (directory / child).write_text("notes", encoding="utf-8")

    with pytest.raises(ValidationError):
        api.load_thresholds(tmp_path / "world")


def test_loader_rejects_empty_or_non_directory_threshold_path(tmp_path: Path) -> None:
    api = thresholds_api()
    empty_world = tmp_path / "empty"
    (empty_world / "data" / "thresholds").mkdir(parents=True)
    with pytest.raises(ValidationError):
        api.load_thresholds(empty_world)

    non_directory_world = tmp_path / "file-world"
    (non_directory_world / "data").mkdir(parents=True)
    (non_directory_world / "data" / "thresholds").write_text("no", encoding="utf-8")
    with pytest.raises(ValidationError):
        api.load_thresholds(non_directory_world)


@pytest.mark.parametrize(
    "definition",
    [
        ("morale", True, 2),
        ("morale", 1, False),
        ("morale", math.nan, 2),
        ("morale", 1, math.inf),
        ("health", 1, 2),
        ("morale", 2, 2),
        ("morale", 3, 2),
    ],
)
def test_injected_registry_is_validated_at_check_boundary(tmp_path: Path, definition) -> None:
    api = thresholds_api()
    record = api.ThresholdDefinition(dial=definition[0], trip=definition[1], rearm=definition[2])
    injected: Mapping[str, Any] = MappingProxyType({"custom_alert": record})

    with pytest.raises(ValidationError):
        api.check_thresholds({"morale": 1}, frozenset(), thresholds=injected)


def test_injected_registry_rejects_invalid_id_and_is_not_mutated(tmp_path: Path) -> None:
    api = thresholds_api()
    injected = MappingProxyType(
        {"Custom-Alert": api.ThresholdDefinition(dial="morale", trip=1, rearm=2)}
    )

    with pytest.raises(ValidationError):
        api.check_thresholds({"morale": 1}, frozenset(), thresholds=injected)
    assert dict(injected) == {
        "Custom-Alert": api.ThresholdDefinition(dial="morale", trip=1, rearm=2)
    }


@pytest.mark.parametrize("dial_value", [True, "20", math.nan, math.inf, -math.inf, None])
def test_check_thresholds_rejects_nonfinite_or_non_numeric_configured_dials(
    tmp_path: Path, dial_value: Any
) -> None:
    api = thresholds_api()
    registry = _registry(tmp_path, {"custom_alert": ("morale", 5, 9)})

    with pytest.raises(ValidationError):
        api.check_thresholds({"morale": dial_value}, frozenset(), thresholds=registry)


@pytest.mark.parametrize(
    ("state", "active"),
    [
        ([], frozenset()),
        ({"morale": 1}, set()),
        ({"morale": 1}, frozenset({1})),
        ({}, frozenset()),
    ],
)
def test_check_thresholds_rejects_invalid_state_active_or_missing_dial(
    tmp_path: Path, state, active
) -> None:
    api = thresholds_api()
    registry = _registry(tmp_path, {"custom_alert": ("morale", 5, 9)})

    with pytest.raises(ValidationError):
        api.check_thresholds(state, active, thresholds=registry)


def test_installed_wheel_loads_bundled_thresholds_outside_checkout(tmp_path: Path) -> None:
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
    wheels = list(wheel_dir.glob("breakroom-*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    with ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "breakroom/data/thresholds/morale_crisis.toml" in names
    assert "breakroom/data/thresholds/budget_crisis.toml" in names

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
    outside = tmp_path / "outside-checkout"
    outside.mkdir()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(installed)
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import json; from breakroom.economy import load_thresholds; "
            "r=load_thresholds(__import__('pathlib').Path('/no/world')); "
            "print(json.dumps({k:[v.dial,v.trip,v.rearm] for k,v in r.items()}, sort_keys=True))",
        ],
        cwd=outside,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "budget_crisis": ["budget", 250, 300],
        "morale_crisis": ["morale", 20, 30],
    }
