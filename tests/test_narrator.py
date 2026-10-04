import json
import shlex
import sys
import time
from subprocess import CompletedProcess, TimeoutExpired

import pytest

from breakroom.narrator import render_scene


def test_render_scene_fallback_when_no_command(monkeypatch):
    monkeypatch.delenv("BREAKROOM_NARRATOR_COMMAND", raising=False)
    monkeypatch.setenv("BREAKROOM_NARRATOR_TIMEOUT", "not-a-number")
    brief = {"character": {"name": "Priya"}, "incident": {"name": "a coffee spill"}}

    result = render_scene(brief)

    assert result == "Priya faced a coffee spill."


def test_render_scene_uses_default_timeout_when_override_is_unset(monkeypatch):
    monkeypatch.setenv("BREAKROOM_NARRATOR_COMMAND", "cat")
    monkeypatch.delenv("BREAKROOM_NARRATOR_TIMEOUT", raising=False)
    call = {}

    def run(*args, **kwargs):
        call.update(kwargs)
        return CompletedProcess(args[0], 0, stdout=" narration ", stderr="")

    monkeypatch.setattr("breakroom.narrator.subprocess.run", run)

    assert render_scene({"character": {"name": "Priya"}, "incident": None}) == "narration"
    assert call["timeout"] == 60.0


@pytest.mark.parametrize("value", ["nope", "0", "-1", "nan", "inf", "-inf"])
def test_render_scene_rejects_invalid_timeout_before_running_command(monkeypatch, value):
    monkeypatch.setenv("BREAKROOM_NARRATOR_COMMAND", "cat")
    monkeypatch.setenv("BREAKROOM_NARRATOR_TIMEOUT", value)
    monkeypatch.setattr(
        "breakroom.narrator.subprocess.run",
        lambda *args, **kwargs: pytest.fail("invalid timeout launched the command"),
    )

    with pytest.raises(RuntimeError, match="BREAKROOM_NARRATOR_TIMEOUT") as excinfo:
        render_scene({"character": {"name": "Priya"}, "incident": None})

    assert value in str(excinfo.value)


def test_render_scene_times_out_real_foreground_command(monkeypatch):
    command = (
        f"{shlex.quote(sys.executable)} -c "
        '"import time; time.sleep(1.5); print(\'late\')"'
    )
    monkeypatch.setenv("BREAKROOM_NARRATOR_COMMAND", command)
    monkeypatch.setenv("BREAKROOM_NARRATOR_TIMEOUT", "0.15")
    started = time.monotonic()

    with pytest.raises(RuntimeError) as excinfo:
        render_scene({"character": {"name": "Priya"}, "incident": None})

    elapsed = time.monotonic() - started
    assert "0.15" in str(excinfo.value)
    assert command in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, TimeoutExpired)
    assert elapsed < 1.0


def test_render_scene_pipes_brief_json_through_command(monkeypatch):
    monkeypatch.setenv("BREAKROOM_NARRATOR_COMMAND", "cat")
    monkeypatch.delenv("BREAKROOM_NARRATOR_TIMEOUT", raising=False)
    brief = {"character": {"name": "Priya"}, "incident": {"name": "a coffee spill"}}

    result = render_scene(brief)

    assert json.loads(result) == brief


def test_render_scene_strips_command_output(monkeypatch):
    monkeypatch.delenv("BREAKROOM_NARRATOR_TIMEOUT", raising=False)
    monkeypatch.setenv(
        "BREAKROOM_NARRATOR_COMMAND",
        "python -c \"print('scripted narration')\"",
    )
    brief = {"character": {"name": "Priya"}, "incident": {"name": "a coffee spill"}}

    result = render_scene(brief)

    assert result == "scripted narration"


@pytest.mark.parametrize("stdout", ["", " \t\n"])
def test_render_scene_rejects_empty_command_output(monkeypatch, stdout):
    monkeypatch.setenv("BREAKROOM_NARRATOR_COMMAND", "configured-narrator")
    monkeypatch.delenv("BREAKROOM_NARRATOR_TIMEOUT", raising=False)
    monkeypatch.setattr(
        "breakroom.narrator.subprocess.run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, stdout=stdout, stderr=""),
    )

    with pytest.raises(
        RuntimeError,
        match="narrator command returned empty output: configured-narrator",
    ):
        render_scene({"character": {"name": "Priya"}, "incident": None})


def test_render_scene_surfaces_stderr_on_failure(monkeypatch):
    monkeypatch.delenv("BREAKROOM_NARRATOR_TIMEOUT", raising=False)
    monkeypatch.setenv(
        "BREAKROOM_NARRATOR_COMMAND",
        "echo 'boom: distinctive narrator failure' 1>&2; exit 1",
    )

    with pytest.raises(RuntimeError) as excinfo:
        render_scene({"character": {"name": "Alex"}, "incident": {"name": "spill"}})

    assert "boom: distinctive narrator failure" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, Exception)
