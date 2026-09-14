"""Pure performance metric functions for simulated return series."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from statistics import stdev

import polars as pl

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class Metrics:
    """Performance metrics for one simulated pipeline."""

    cumulative_return: float | None
    cagr: float | None
    annualized_volatility: float | None
    sharpe_ratio: float | None
    max_drawdown: float | None
    calmar_ratio: float | None
    win_rate: float | None
    profit_factor: float | None
    mean_turnover: float | None

    def as_dict(self) -> dict[str, float | None]:
        """Return metrics using their stable public names."""
        return asdict(self)


def cumulative_return(equity: Sequence[float]) -> float | None:
    """Return total equity growth, or ``None`` for an empty series."""
    if not equity or not _all_finite(equity):
        return None
    return float(equity[-1] - 1.0)


def cagr(
    equity: Sequence[float],
    start: date | datetime | None,
    end: date | datetime | None,
) -> float | None:
    """Return time-based CAGR using 365.25 days per year."""
    if not equity or not _all_finite(equity) or start is None or end is None:
        return None
    elapsed_days = (end - start).total_seconds() / 86_400.0
    if elapsed_days <= 0.0 or equity[0] <= 0.0 or equity[-1] <= 0.0:
        return None
    years = elapsed_days / 365.25
    return float((equity[-1] / equity[0]) ** (1.0 / years) - 1.0)


def annualized_volatility(
    returns: Sequence[float], periods_per_year: int = TRADING_DAYS_PER_YEAR
) -> float | None:
    """Return sample return volatility annualized by ``sqrt(periods_per_year)``."""
    _validate_periods(periods_per_year)
    if len(returns) < 2 or not _all_finite(returns):
        return None
    return float(stdev(returns) * math.sqrt(periods_per_year))


def sharpe_ratio(
    returns: Sequence[float], periods_per_year: int = TRADING_DAYS_PER_YEAR
) -> float | None:
    """Return zero-risk-free Sharpe ratio, or ``None`` for zero volatility."""
    volatility = annualized_volatility(returns, periods_per_year)
    if volatility is None or volatility <= 1e-12:
        return None
    mean_return = sum(returns) / len(returns)
    return float(mean_return * periods_per_year / volatility)


def max_drawdown(equity: Sequence[float]) -> float | None:
    """Return the minimum peak-to-trough percentage drawdown."""
    if not equity or not _all_finite(equity):
        return None
    peak = equity[0]
    drawdowns: list[float] = []
    for value in equity:
        peak = max(peak, value)
        if peak <= 0.0:
            return None
        drawdowns.append(value / peak - 1.0)
    return float(min(drawdowns))


def calmar_ratio(cagr_value: float | None, drawdown: float | None) -> float | None:
    """Return CAGR divided by absolute maximum drawdown."""
    if cagr_value is None or drawdown is None or abs(drawdown) <= 1e-12:
        return None
    return float(cagr_value / abs(drawdown))


def win_rate(returns: Sequence[float]) -> float | None:
    """Return the fraction of periods with strictly positive returns."""
    if not returns or not _all_finite(returns):
        return None
    return float(sum(value > 0.0 for value in returns) / len(returns))


def profit_factor(returns: Sequence[float]) -> float | None:
    """Return gross gains divided by absolute gross losses."""
    if not returns or not _all_finite(returns):
        return None
    gains = sum(value for value in returns if value > 0.0)
    losses = sum(value for value in returns if value < 0.0)
    if abs(losses) <= 1e-12:
        return None
    return float(gains / abs(losses))


def mean_turnover(turnover: Sequence[float]) -> float | None:
    """Return average turnover per simulated period."""
    if not turnover or not _all_finite(turnover):
        return None
    return float(sum(turnover) / len(turnover))


def compute_metrics(
    simulation: pl.DataFrame,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    initial_capital: float = 1.0,
) -> Metrics:
    """Compute all tear-sheet metrics from a simulation DataFrame."""
    required = {"observation_timestamp", "net_return", "equity", "turnover"}
    missing = sorted(required - set(simulation.columns))
    if missing:
        raise ValueError(f"Simulation missing required metric columns: {missing}")
    if initial_capital <= 0.0:
        raise ValueError("initial_capital must be greater than zero.")
    if simulation.is_empty():
        return Metrics(*(None for _ in range(9)))

    ordered = simulation.sort("observation_timestamp")
    returns = [float(value) for value in ordered["net_return"].to_list()]
    equity = [float(value) for value in ordered["equity"].to_list()]
    turnover = [float(value) for value in ordered["turnover"].to_list()]
    timestamps = ordered["observation_timestamp"].to_list()
    growth = None if not equity or not _all_finite(equity) else equity[-1] / initial_capital - 1.0
    annualized_cagr = cagr([initial_capital, equity[-1]], timestamps[0], timestamps[-1])
    drawdown = max_drawdown(equity)
    return Metrics(
        cumulative_return=growth,
        cagr=annualized_cagr,
        annualized_volatility=annualized_volatility(returns, periods_per_year),
        sharpe_ratio=sharpe_ratio(returns, periods_per_year),
        max_drawdown=drawdown,
        calmar_ratio=calmar_ratio(annualized_cagr, drawdown),
        win_rate=win_rate(returns),
        profit_factor=profit_factor(returns),
        mean_turnover=mean_turnover(turnover),
    )


def _all_finite(values: Sequence[float]) -> bool:
    return all(math.isfinite(value) for value in values)


def _validate_periods(periods_per_year: int) -> None:
    if periods_per_year < 1:
        raise ValueError("periods_per_year must be at least 1.")


__all__ = [
    "Metrics",
    "TRADING_DAYS_PER_YEAR",
    "annualized_volatility",
    "cagr",
    "calmar_ratio",
    "compute_metrics",
    "cumulative_return",
    "max_drawdown",
    "mean_turnover",
    "profit_factor",
    "sharpe_ratio",
    "win_rate",
]
