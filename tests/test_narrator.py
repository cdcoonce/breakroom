import json

import pytest

from breakroom.narrator import render_scene


def test_render_scene_fallback_when_no_command(monkeypatch):
    monkeypatch.delenv("BREAKROOM_NARRATOR_COMMAND", raising=False)
    brief = {"character": {"name": "Priya"}, "incident": {"name": "a coffee spill"}}

    result = render_scene(brief)

    assert result == "Priya faced a coffee spill."


def test_render_scene_pipes_brief_json_through_command(monkeypatch):
    monkeypatch.setenv("BREAKROOM_NARRATOR_COMMAND", "cat")
    brief = {"character": {"name": "Priya"}, "incident": {"name": "a coffee spill"}}

    result = render_scene(brief)

    assert json.loads(result) == brief


def test_render_scene_strips_command_output(monkeypatch):
    monkeypatch.setenv(
        "BREAKROOM_NARRATOR_COMMAND",
        "python -c \"print('scripted narration')\"",
    )
    brief = {"character": {"name": "Priya"}, "incident": {"name": "a coffee spill"}}

    result = render_scene(brief)

    assert result == "scripted narration"


def test_render_scene_surfaces_stderr_on_failure(monkeypatch):
    monkeypatch.setenv(
        "BREAKROOM_NARRATOR_COMMAND",
        "echo 'boom: distinctive narrator failure' 1>&2; exit 1",
    )

    with pytest.raises(RuntimeError) as excinfo:
        render_scene({"character": {"name": "Alex"}, "incident": {"name": "spill"}})

    assert "boom: distinctive narrator failure" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, Exception)
