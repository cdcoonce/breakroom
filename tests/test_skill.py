"""Drift checks between skills/breakroom/SKILL.md and the real CLI surface."""

from __future__ import annotations

import io
import re
from argparse import _SubParsersAction
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from breakroom.cli import build_parser

SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "breakroom" / "SKILL.md"
INVOCATION_PREFIX = "uv run breakroom "
COMMAND_LINE = re.compile(r"^(?:uv run )?breakroom\s+(\S+)(.*)$")
INLINE_COMMAND = re.compile(r"`(?:uv run )?breakroom\s+([^`]+)`")
FLAG = re.compile(r"--[A-Za-z][A-Za-z0-9-]*")
EXPERIMENTER_VERBS = ["init", "tick", "build", "hire", "direct", "read", "status"]


def _skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _help_text(args: list[str]) -> str:
    parser = build_parser()
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit):
        parser.parse_args([*args, "--help"])
    return buf.getvalue()


def _top_level_subcommands() -> set[str]:
    # Read the subparser choices directly rather than scraping the usage line: once
    # more subcommands land, argparse wraps usage and the `{...}` group can move off
    # the first line, which would fail this check on a perfectly healthy CLI.
    for action in build_parser()._actions:
        if isinstance(action, _SubParsersAction):
            return set(action.choices)
    raise AssertionError("`breakroom --help` exposes no subcommands at all")


def _command_lines() -> list[re.Match[str]]:
    matches = [
        COMMAND_LINE.match(line.strip())
        for line in _skill_text().splitlines()
        if line.strip().startswith(("breakroom ", "uv run breakroom "))
    ]
    return [match for match in matches if match]


def _check_invocation(subcommand: str, rest: str) -> None:
    if subcommand.startswith("-"):
        # A bare `breakroom --flag` invocation: validate against the top-level help.
        for flag in FLAG.findall(f"{subcommand} {rest}"):
            assert flag in _help_text([]), (
                f"SKILL.md references top-level `{flag}`, which is not in `breakroom --help`"
            )
        return
    subcommands = _top_level_subcommands()
    assert subcommand in subcommands, (
        f"SKILL.md references `breakroom {subcommand}`, which is not in "
        f"`breakroom --help`'s subcommand list {sorted(subcommands)}"
    )
    subcommand_help = _help_text([subcommand])
    for flag in FLAG.findall(rest):
        assert flag in subcommand_help, (
            f"SKILL.md references `{flag}` on `breakroom {subcommand}`, which is not "
            f"in `breakroom {subcommand} --help`"
        )


def test_skill_file_exists() -> None:
    assert SKILL_PATH.is_file()


def test_every_documented_command_uses_the_uv_run_invocation() -> None:
    # `breakroom` is a project console script, so on a fresh clone it is only
    # reachable via `uv run` (see README.md's quickstart). A bare `breakroom ...`
    # in the skill would send a cold agent straight into command-not-found.
    bare = [
        line.strip()
        for line in _skill_text().splitlines()
        if line.strip().startswith("breakroom ")
    ]
    assert not bare, f"SKILL.md documents commands without the `{INVOCATION_PREFIX}` prefix: {bare}"
    assert INVOCATION_PREFIX in _skill_text()
    assert "uv sync" in _skill_text(), "SKILL.md must name the `uv sync` bootstrap step"


def test_every_skill_command_line_matches_real_help_output() -> None:
    command_lines = _command_lines()
    assert command_lines, "expected at least one literal `breakroom ...` command line"
    for match in command_lines:
        _check_invocation(match.group(1), match.group(2))


def test_inline_command_mentions_name_no_invented_flags() -> None:
    # Inline backtick mentions may legitimately name a subcommand that does not exist
    # yet: the issue asks this skill to state missing verbs (`read`, `direct`, ...) as
    # gaps rather than silently omitting them. So unknown subcommands are skipped here,
    # but any flag hung off a subcommand that *does* exist must be real.
    subcommands = _top_level_subcommands()
    for mention in INLINE_COMMAND.findall(_skill_text()):
        subcommand, _, rest = mention.partition(" ")
        if subcommand not in subcommands:
            continue
        subcommand_help = _help_text([subcommand])
        for flag in FLAG.findall(rest):
            assert flag in subcommand_help, (
                f"SKILL.md names `{flag}` on `breakroom {subcommand}` in prose, which is "
                f"not in `breakroom {subcommand} --help`"
            )


def test_every_experimenter_verb_is_mentioned() -> None:
    text = _skill_text()
    missing = [verb for verb in EXPERIMENTER_VERBS if f"`{verb}`" not in text]
    assert not missing, f"SKILL.md never mentions these experimenter verbs: {missing}"


def test_sealed_store_prohibition_is_explicit() -> None:
    text = _skill_text()
    assert ".secrets/" in text
    index = text.index(".secrets/")
    window = text[max(0, index - 200) : index + 200]
    assert re.search(r"\bnever\b", window, re.IGNORECASE), (
        "expected 'never' near the `.secrets/` mention to state the prohibition plainly"
    )
