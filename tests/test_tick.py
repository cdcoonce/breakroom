from pathlib import Path

from breakroom.tick import load_character


def test_load_character_id_comes_from_the_filename_not_the_toml_body(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    (world / "characters").mkdir(parents=True)
    (world / "characters" / "avery.toml").write_text(
        'id = "someone-else"\nname = "Avery"\nmodel = "stub"\n\n[stats]\n'
    )

    character = load_character(world, "avery")

    assert character["id"] == "avery"
