"""The tidewater command: plan a day, or assign one vessel."""

import argparse
from datetime import date

from tidewater.berths import BerthAllocator, NoBerthAvailable, Vessel


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tidewater")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="print the berth plan of a day")
    plan.add_argument("--day", type=date.fromisoformat, required=True)
    assign = commands.add_parser("assign", help="assign one arriving vessel")
    assign.add_argument("vessel")
    assign.add_argument("--length", type=float, required=True)
    assign.add_argument("--draught", type=float, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    allocator = BerthAllocator()
    if args.command == "assign":
        try:
            berth = allocator.allocate(Vessel(args.vessel, args.length, args.draught))
        except NoBerthAvailable as error:
            print(f"cannot assign: {error}")
            return 1
        print(f"{args.vessel} -> berth {berth.number}")
    return 0
