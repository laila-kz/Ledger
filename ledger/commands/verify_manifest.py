"""Manifest verification command placeholder for Week 5 Day 3."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledger verify-manifest",
        description="Verify a Ledger run manifest (implemented in Week 5 Day 3).",
        epilog="Examples:\n  ledger verify-manifest artifacts/runs/<run_id>/manifest.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("manifest", nargs="?", help="Path to manifest.json.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse verification arguments; full cryptographic checks arrive on Day 3."""
    try:
        args = build_parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 1
    if args.manifest is None:
        build_parser().print_help()
        return 0
    raise NotImplementedError("Manifest verification is scheduled for Week 5 Day 3.")
