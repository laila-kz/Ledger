"""Console wrapper for the leakage canary suite."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``tests/canaries`` with the current Python interpreter."""
    if argv and any(argument in {"-h", "--help"} for argument in argv):
        print(
            "usage: ledger canaries\n\n"
            "Run the Leakage Canary Suite.\n\n"
            "Examples:\n  ledger canaries"
        )
        return 0
    command = [sys.executable, "-m", "pytest", "tests/canaries", "-v"]
    if argv:
        command.extend(argv)
    completed = subprocess.run(command, check=False)
    return completed.returncode
