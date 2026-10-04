from __future__ import annotations

import argparse
import json
from pathlib import Path

from breakroom import economy
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

    contracts = subparsers.add_parser("contracts", help="Inspect and manage contract offers.")
    contract_commands = contracts.add_subparsers(dest="contracts_command", required=True)
    contract_list = contract_commands.add_parser("list", help="List offers and active contracts.")
    contract_list.add_argument("--world", type=Path, default=Path("."))
    contract_accept = contract_commands.add_parser("accept", help="Accept an offer.")
    contract_accept.add_argument("offer_id")
    contract_accept.add_argument("--team", nargs="+", required=True)
    contract_accept.add_argument("--room")
    contract_accept.add_argument("--world", type=Path, default=Path("."))
    contract_decline = contract_commands.add_parser("decline", help="Decline an offer.")
    contract_decline.add_argument("offer_id")
    contract_decline.add_argument("--world", type=Path, default=Path("."))

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        init_world(args.world, seed=args.seed)
        return 0
    if args.command == "tick":
        tick_world(args.world)
        return 0
    if args.command == "contracts":
        if args.contracts_command == "list":
            print(json.dumps(economy.list_contracts(args.world), indent=2, sort_keys=True))
        elif args.contracts_command == "accept":
            print(
                json.dumps(
                    economy.accept_contract(args.world, args.offer_id, args.team, args.room),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.contracts_command == "decline":
            print(
                json.dumps(
                    economy.decline_contract(args.world, args.offer_id), indent=2, sort_keys=True
                )
            )
        return 0
    raise AssertionError(f"unhandled command: {args.command}")
