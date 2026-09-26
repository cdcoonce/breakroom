from __future__ import annotations

import json
from pathlib import Path

from breakroom.events import append_event


def test_append_event_starts_sequence_at_one_for_missing_log(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    world.mkdir()

    result = append_event(world, {"type": "note"})

    assert result["sequence"] == 1
    assert result["type"] == "note"


def test_append_event_starts_sequence_at_one_for_empty_log(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    world.mkdir()
    (world / "events.jsonl").write_text("", encoding="utf-8")

    result = append_event(world, {"type": "note"})

    assert result["sequence"] == 1


def test_append_event_increments_sequence_across_appends(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    world.mkdir()

    first = append_event(world, {"type": "note"})
    second = append_event(world, {"type": "note"})
    third = append_event(world, {"type": "note"})

    assert (first["sequence"], second["sequence"], third["sequence"]) == (1, 2, 3)


def test_append_event_ignores_blank_lines_when_counting(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    world.mkdir()
    events_path = world / "events.jsonl"
    events_path.write_text(
        json.dumps({"sequence": 1, "type": "note"}, sort_keys=True)
        + "\n\n\n"
        + json.dumps({"sequence": 2, "type": "note"}, sort_keys=True)
        + "\n\n",
        encoding="utf-8",
    )

    result = append_event(world, {"type": "note"})

    assert result["sequence"] == 3


def test_append_event_cannot_be_overridden_by_caller_supplied_sequence(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    world.mkdir()
    append_event(world, {"type": "note"})

    result = append_event(world, {"type": "note", "sequence": 999})

    assert result["sequence"] == 2


def test_append_event_reads_only_the_tail_of_a_large_log(tmp_path: Path, monkeypatch) -> None:
    world = tmp_path / "tower"
    world.mkdir()
    events_path = world / "events.jsonl"
    lines = [
        json.dumps({"sequence": n, "type": "note"}, sort_keys=True) for n in range(1, 5001)
    ]
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    file_size = events_path.stat().st_size

    bytes_read = 0
    real_open = Path.open

    def spy_open(self: Path, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self == events_path:
            real_read = handle.read

            def counted_read(size=-1):
                nonlocal bytes_read
                data = real_read(size)
                bytes_read += len(data)
                return data

            handle.read = counted_read
        return handle

    monkeypatch.setattr(Path, "open", spy_open)

    result = append_event(world, {"type": "note"})

    assert result["sequence"] == 5001
    assert bytes_read < file_size // 10
