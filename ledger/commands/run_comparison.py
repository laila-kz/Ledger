"""Console wrapper for the existing comparative backtest CLI."""

from __future__ import annotations

from collections.abc import Sequence

from ledger.backtest.run_comparison import main as _backtest_main


def main(argv: Sequence[str] | None = None) -> int:
    """Forward command arguments to the backtest implementation."""
    try:
        return _backtest_main(list(argv) if argv is not None else None)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 1
