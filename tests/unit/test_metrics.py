"""Unit tests for Day 3 performance metrics."""

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from ledger.backtest.metrics import (
    annualized_volatility,
    calmar_ratio,
    compute_metrics,
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    win_rate,
)

UTC = timezone.utc


def test_metric_formulas_and_guards() -> None:
    returns = [0.10, -0.05, 0.02]
    assert annualized_volatility(returns, periods_per_year=1) == pytest.approx(0.0750555, rel=1e-5)
    assert sharpe_ratio(returns, periods_per_year=1) == pytest.approx(
        sum(returns) / 3 / annualized_volatility(returns, 1)
    )
    assert max_drawdown([1.1, 1.045, 1.0659]) == pytest.approx(-0.05)
    assert calmar_ratio(0.12, -0.20) == pytest.approx(0.6)
    assert win_rate(returns) == pytest.approx(2 / 3)
    assert profit_factor(returns) == pytest.approx(2.4)
    assert sharpe_ratio([0.01, 0.01]) is None
    assert profit_factor([0.01, 0.02]) is None


def test_compute_metrics_uses_timestamp_years() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    simulation = pl.DataFrame(
        {
            "observation_timestamp": [start, start + timedelta(days=365)],
            "net_return": [0.10, 0.10],
            "equity": [1.10, 1.21],
            "turnover": [1.0, 0.1],
        }
    )

    result = compute_metrics(simulation, periods_per_year=1)

    assert result.cumulative_return == pytest.approx(0.21)
    assert result.cagr == pytest.approx(1.21 ** (365.25 / 365.0) - 1.0)
    assert result.mean_turnover == pytest.approx(0.55)
