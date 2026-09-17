"""Manifest verification command for Week 5 Day 3."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ledger.lineage.manifest import verify_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledger verify-manifest",
        description="Verify a Ledger run manifest for data integrity and reproducibility.",
        epilog=(
            "Examples:\n"
            "  ledger verify-manifest artifacts/runs/<run_id>/manifest.json\n"
            "  ledger verify-manifest --repo-root . artifacts/runs/abc123/manifest.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("manifest", nargs="?", help="Path to manifest.json file to verify.")
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root for relative path resolution (default: current directory).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Verify a manifest by re-hashing inputs, features, and lockfile."""
    try:
        args = build_parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 1

    if args.manifest is None:
        build_parser().print_help()
        return 0

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"Error: Manifest file not found: {manifest_path}", file=sys.stderr)
        return 1

    result = verify_manifest(manifest_path, repo_root=args.repo_root)
    output = str(result)
    try:
        print(output)
    except UnicodeEncodeError:
        safe_output = output.encode(sys.stdout.encoding or "ascii", errors="replace").decode(
            sys.stdout.encoding or "ascii"
        )
        print(safe_output)

    return 0 if result.passed else 1
