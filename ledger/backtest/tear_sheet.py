"""Comparative tear-sheet orchestration and leakage attribution."""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from .formatting import render_comparison
from .metrics import TRADING_DAYS_PER_YEAR, Metrics, compute_metrics


@dataclass(frozen=True)
class TearSheet:
    """Structured leaky-versus-corrected performance report."""

    leaky: Metrics
    corrected: Metrics
    delta: Metrics

    def render(self) -> str:
        """Render the differential report for terminal output."""
        return render_comparison(self.leaky, self.corrected, self.delta)


def build_tear_sheet(
    leaky_simulation: pl.DataFrame,
    corrected_simulation: pl.DataFrame,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    initial_capital: float = 1.0,
) -> TearSheet:
    """Compute both metric sets and corrected-minus-leaky attribution."""
    leaky = compute_metrics(leaky_simulation, periods_per_year, initial_capital)
    corrected = compute_metrics(corrected_simulation, periods_per_year, initial_capital)
    delta = Metrics(
        **{
            field: _difference(getattr(corrected, field), getattr(leaky, field))
            for field in corrected.__dataclass_fields__
        }
    )
    return TearSheet(leaky=leaky, corrected=corrected, delta=delta)


def _difference(corrected: float | None, leaky: float | None) -> float | None:
    if corrected is None or leaky is None:
        return None
    return corrected - leaky


__all__ = ["TearSheet", "build_tear_sheet"]
