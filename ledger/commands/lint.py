"""Command-line adapter for the static leakage linter."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from ledger.tools.linter import lint_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledger lint",
        description="Static leakage checks for a quant alpha script.",
        epilog="Examples:\n  ledger lint examples/sample_strategy.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("script", nargs="?", help="Python alpha script to inspect.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Lint one Python script and return a CI-friendly exit code."""
    try:
        args = build_parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 1
    if args.script is None:
        build_parser().print_help()
        return 0

    path = Path(args.script)
    if not path.exists():
        print(f"Error: script file not found: {path}")
        return 1

    try:
        findings = lint_file(path)
    except SyntaxError as error:
        print(f"{path}:{error.lineno}: syntax error: {error.msg}")
        return 1

    print(f"Ledger Leakage Linter: {path}")
    if not findings:
        print("PASS: no known leakage patterns detected")
        return 0

    for finding in findings:
        print(f"\n{finding.rule} at {path}:{finding.lineno}:{finding.col_offset + 1}")
        print(f"  {finding.message}")
        print(f"  Code: {finding.snippet}")
        print(f"  Advice: {finding.advice}")
    print(f"\nFAIL: {len(findings)} potential leakage pattern(s) detected")
    return 1
