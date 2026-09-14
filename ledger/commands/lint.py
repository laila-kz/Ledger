"""Static leakage linter command placeholder for Week 5 Day 4."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledger lint",
        description="Static leakage checks for an alpha script (coming Day 4).",
        epilog="Examples:\n  ledger lint examples/sample_strategy.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("script", nargs="?", help="Python alpha script to inspect.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse linter arguments; AST rules arrive on Week 5 Day 4."""
    try:
        args = build_parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 1
    if args.script is None:
        build_parser().print_help()
        return 0
    raise NotImplementedError("The AST leakage linter is scheduled for Week 5 Day 4.")
