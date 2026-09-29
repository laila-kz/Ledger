"""Canary 07 – Synthetic demo sanity invariants.

This test runs the full ``--synthetic`` comparative backtest with the same
parameters that ``ledger run-comparison --synthetic`` uses in the demo, and
asserts structural inequalities that *must* hold for the demo to be credible:

    1. leaky_sharpe   > corrected_sharpe  (leakage inflates risk-adjusted return)
    2. leaky_cumret   > corrected_cumret  (leakage inflates headline return)
    3. |leaky_mdd|    <= |corrected_mdd|  (leakage masks drawdown risk)
    4. corrected_sharpe is finite and positive (corrected pipeline is profitable
       over the full GBM path with fixed seed)

Failures here mean either the generator produces unrealistic data or the
pipeline logic has regressed.

NOTE: Numbers depend on PRNG_SEED in ledger/backtest/synthetic.py.  If the
seed is changed, re-run this test to verify invariants still hold.
"""

from datetime import date, datetime, time, timezone

import polars as pl
import pytest

from ledger.backtest.metrics import TRADING_DAYS_PER_YEAR, compute_metrics
from ledger.backtest.runner import run_comparison
from ledger.backtest.simulation import SimulationConfig
from ledger.backtest.strategy import StrategyConfig
from ledger.backtest.synthetic import generate_synthetic_data

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Parameters matching the CLI demo invocation
# ---------------------------------------------------------------------------
_START = date(2020, 1, 1)
_END = date(2023, 12, 31)
_SEC_IDS = (
    "SEC_AAPL_001",
    "SEC_MSFT_001",
    "SEC_NVDA_001",
    "SEC_META_001",
    "SEC_GOOGL_001",
)
_TICKERS = tuple(s.split("_")[1] for s in _SEC_IDS)


def _build_observations(raw_prices: pl.DataFrame) -> pl.DataFrame:
    dates = raw_prices.select("trade_date").unique().sort("trade_date")["trade_date"].to_list()
    timestamps = [datetime.combine(d, time(21, 5), tzinfo=UTC) for d in dates]
    return pl.DataFrame(
        {
            "sec_id": [s for s in _SEC_IDS for _ in timestamps],
            "observation_timestamp": timestamps * len(_SEC_IDS),
        }
    )


@pytest.fixture(scope="module")
def comparison_result():
    """Run the full synthetic demo comparison once per test module."""
    raw_prices, preadjusted, splits = generate_synthetic_data(_SEC_IDS, _START, _END)
    observations = _build_observations(raw_prices)
    return run_comparison(
        observation_matrix=observations,
        raw_prices=raw_prices,
        preadjusted_prices=preadjusted,
        splits=splits,
        strategy_config=StrategyConfig(top_n=3),
        simulation_config=SimulationConfig(transaction_cost_bps=5.0),
        leaky_universe=_SEC_IDS,
    )


@pytest.fixture(scope="module")
def leaky_metrics(comparison_result):
    return compute_metrics(
        comparison_result.leaky.simulation,
        periods_per_year=TRADING_DAYS_PER_YEAR,
    )


@pytest.fixture(scope="module")
def corrected_metrics(comparison_result):
    return compute_metrics(
        comparison_result.corrected.simulation,
        periods_per_year=TRADING_DAYS_PER_YEAR,
    )


# ---------------------------------------------------------------------------
# Invariant 1: leakage inflates Sharpe
# ---------------------------------------------------------------------------
def test_leaky_sharpe_exceeds_corrected(leaky_metrics, corrected_metrics):
    """The leaky pipeline must report a *higher* Sharpe than the corrected one.

    This is the central claim of the demo: feeding a strategy future-adjusted
    prices (as-of-day-open) gives it information it could not have had at the
    observation timestamp, artificially boosting measured risk-adjusted returns.
    """
    leaky_s = leaky_metrics.sharpe_ratio
    corrected_s = corrected_metrics.sharpe_ratio
    assert leaky_s is not None, "Leaky Sharpe must be computable"
    assert corrected_s is not None, "Corrected Sharpe must be computable"
    assert leaky_s > corrected_s, (
        f"Leakage should inflate Sharpe: leaky={leaky_s:.4f}, corrected={corrected_s:.4f}"
    )


# ---------------------------------------------------------------------------
# Invariant 2: leakage inflates cumulative return
# ---------------------------------------------------------------------------
def test_leaky_cumret_exceeds_corrected(leaky_metrics, corrected_metrics):
    """The leaky pipeline must report a *higher* cumulative return."""
    leaky_r = leaky_metrics.cumulative_return
    corrected_r = corrected_metrics.cumulative_return
    assert leaky_r is not None, "Leaky cumulative return must be computable"
    assert corrected_r is not None, "Corrected cumulative return must be computable"
    assert leaky_r > corrected_r, (
        f"Leakage should inflate return: leaky={leaky_r:.4f}, corrected={corrected_r:.4f}"
    )


# ---------------------------------------------------------------------------
# Invariant 3: leakage masks drawdown risk
# ---------------------------------------------------------------------------
def test_leaky_drawdown_smaller_in_absolute_terms(leaky_metrics, corrected_metrics):
    """The corrected pipeline must exhibit *at least as large* a max drawdown.

    Drawdown is returned as a negative number; smaller (more negative) means
    worse.  Leakage hides dips because future-adjusted prices don't exhibit
    the volatility the portfolio actually experienced.
    """
    leaky_mdd = leaky_metrics.max_drawdown
    corrected_mdd = corrected_metrics.max_drawdown
    assert leaky_mdd is not None, "Leaky MDD must be computable"
    assert corrected_mdd is not None, "Corrected MDD must be computable"
    # |corrected_mdd| >= |leaky_mdd|  ↔  corrected_mdd <= leaky_mdd
    assert corrected_mdd <= leaky_mdd, (
        f"Corrected pipeline should show worse (larger) drawdown: "
        f"leaky={leaky_mdd:.4f}, corrected={corrected_mdd:.4f}"
    )


# ---------------------------------------------------------------------------
# Invariant 4: corrected pipeline is economically meaningful
# ---------------------------------------------------------------------------
def test_corrected_sharpe_is_positive(corrected_metrics):
    """The corrected pipeline must have a positive Sharpe over the 4-year window.

    This validates that the GBM path (with fixed seed) produces a dataset
    where momentum is profitable.  If this fails, change the seed and
    re-document.
    """
    corrected_s = corrected_metrics.sharpe_ratio
    assert corrected_s is not None, "Corrected Sharpe must be computable"
    assert corrected_s > 0.0, (
        f"Corrected Sharpe should be positive (strategy profitable on this path): "
        f"got {corrected_s:.4f}"
    )
