from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from breakroom import jsonio
from breakroom.init import init_world
from breakroom.jsonio import write_pretty_json
from breakroom.secrets import seal_secret
from breakroom.tick import tick_world
from breakroom.worldstate import write_snapshot


def test_write_pretty_json_produces_sorted_indented_bytes_with_trailing_newline(
    tmp_path: Path,
) -> None:
    path = tmp_path / "out.json"
    obj = {"b": 1, "a": {"z": 2, "y": [3, 2, 1]}}

    write_pretty_json(path, obj)

    assert path.read_bytes() == (
        b'{\n  "a": {\n    "y": [\n      3,\n      2,\n      1\n    ],\n    "z": 2\n  },'
        b'\n  "b": 1\n}\n'
    )


def test_write_snapshot_calls_write_pretty_json(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[Path, object]] = []
    monkeypatch.setattr(jsonio, "write_pretty_json", lambda path, obj: calls.append((path, obj)))
    world = tmp_path / "tower"
    world.mkdir()
    state = {"day": 0}

    write_snapshot(world, state, "day-0000")

    assert len(calls) == 1
    written_path, written_obj = calls[0]
    assert written_path == world / "snapshots" / "day-0000.json"
    assert written_obj == state


def test_init_world_calls_write_pretty_json(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[Path, object]] = []
    monkeypatch.setattr(jsonio, "write_pretty_json", lambda path, obj: calls.append((path, obj)))
    world = tmp_path / "tower"

    init_world(world, seed=42)

    assert len(calls) == 1
    written_path, written_obj = calls[0]
    assert written_path == world / "state" / "tower.json"
    assert written_obj["seed"] == 42


def test_tick_world_calls_write_pretty_json(tmp_path: Path, monkeypatch) -> None:
    world = tmp_path / "tower"
    init_world(world, seed=1)

    calls: list[tuple[Path, object]] = []
    monkeypatch.setattr(jsonio, "write_pretty_json", lambda path, obj: calls.append((path, obj)))

    tick_world(world)

    assert len(calls) == 1
    written_path, written_obj = calls[0]
    assert written_path == world / "state" / "tower.json"
    assert written_obj["day"] == 1


def test_save_store_calls_write_pretty_json(tmp_path: Path, monkeypatch) -> None:
    world = tmp_path / "tower"
    world.mkdir()
    (world / "events.jsonl").write_text("", encoding="utf-8")

    calls: list[tuple[Path, object]] = []
    monkeypatch.setattr(jsonio, "write_pretty_json", lambda path, obj: calls.append((path, obj)))

    seal_secret(
        world,
        id="affair-1",
        holder="jordan-vale",
        content="Jordan is quietly job-hunting.",
        is_true=True,
    )

    assert len(calls) == 1
    written_path, written_obj = calls[0]
    assert written_path == world / ".secrets" / world.name / "secrets.json"
    assert "affair-1" in written_obj


def test_write_pretty_json_atomic_does_not_corrupt_existing_file_on_write_failure(
    tmp_path: Path,
) -> None:
    """If write fails partway through, the destination file must be untouched."""
    path = tmp_path / "out.json"
    original_content = '{"seed": 42}\n'
    path.write_text(original_content, encoding="utf-8")

    # Simulate a write that fails after the temp file is created but before replace.
    original_replace = os.replace

    def failing_replace(src, dst):
        # After replace is called, the real file is already written.
        # Fail before calling replace to simulate crash during write.
        raise OSError("simulated write failure")

    with patch.object(os, "replace", failing_replace):
        try:
            write_pretty_json(path, {"seed": 99})
        except OSError:
            pass

    # The destination must be untouched — either original content or absent.
    # os.replace was never called so tmp file is cleaned up by tempfile semantics,
    # but in case delete=False was used, check the destination is not corrupted.
    if path.exists():
        assert path.read_text(encoding="utf-8") == original_content
