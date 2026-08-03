from __future__ import annotations

import argparse
from pathlib import Path

from breakroom.init import init_world
from breakroom.tick import tick_world


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="breakroom")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Create a starter tower world.")
    init.add_argument("--world", type=Path, default=Path("."))
    init.add_argument("--seed", type=int, default=1)

    tick = subparsers.add_parser("tick", help="Advance the tower by one workday.")
    tick.add_argument("--world", type=Path, default=Path("."))

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        init_world(args.world, seed=args.seed)
        return 0
    if args.command == "tick":
        tick_world(args.world)
        return 0
    raise AssertionError(f"unhandled command: {args.command}")
