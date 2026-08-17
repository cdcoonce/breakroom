import pytest

from breakroom.narrator import render_scene


def test_render_scene_surfaces_stderr_on_failure(monkeypatch):
    monkeypatch.setenv(
        "BREAKROOM_NARRATOR_COMMAND",
        "echo 'boom: distinctive narrator failure' 1>&2; exit 1",
    )

    with pytest.raises(RuntimeError) as excinfo:
        render_scene({"character": {"name": "Alex"}, "incident": {"name": "spill"}})

    assert "boom: distinctive narrator failure" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, Exception)
