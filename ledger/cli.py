"""Top-level console dispatcher for Ledger developer and backtest commands."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import ledger
from ledger.commands import canaries, lint, run_comparison, verify_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledger",
        description="Ledger quant data tooling.",
        epilog=(
            "Examples:\n"
            "  ledger canaries\n"
            "  ledger run-comparison --start-date 2018-01-01 --end-date 2023-12-31"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=ledger.__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("lint", "Static leakage checks for an alpha script."),
        ("verify-manifest", "Verify a generated run manifest."),
        ("run-comparison", "Run the leaky versus corrected backtest."),
        ("canaries", "Run the leakage canary test suite."),
    ):
        subparsers.add_parser(command, help=help_text, add_help=False)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a Ledger subcommand and return its process exit code."""
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if not arguments or arguments[0] in {"-h", "--help"}:
        build_parser().print_help()
        return 0
    if arguments[0] == "--version":
        print(ledger.__version__)
        return 0

    command, *command_args = arguments
    handlers = {
        "lint": lint.main,
        "verify-manifest": verify_manifest.main,
        "run-comparison": run_comparison.main,
        "canaries": canaries.main,
    }
    handler = handlers.get(command)
    if handler is None:
        build_parser().error(f"argument command: invalid choice: {command!r}")
    return handler(command_args)


if __name__ == "__main__":
    raise SystemExit(main())
