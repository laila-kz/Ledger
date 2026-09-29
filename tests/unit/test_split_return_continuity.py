"""Regression tests for corporate-action (split) return continuity.

Why these tests exist
---------------------
An earlier demo fixture multiplied pre-split prices by the split ratio *and*
stored raw prices that were already 4x elevated, producing a spurious 16:1
price cliff at the split date. That cliff showed up as a ~-93% single-day
asset return and a ~-31% portfolio drawdown which was then mis-reported as
evidence of lookahead bias. It was a bug in the demo, not leakage.

The invariant a split must satisfy is narrow and testable: adjusting for a
split changes price *levels*, not economic *returns*. A correct point-in-time
CAF therefore leaves the return series continuous across the ex-date.

These tests assert that invariant directly instead of asserting that every
daily return is positive. A monotone "all returns >= 0" assertion is only
satisfiable by a deterministic, noise-free price path; it encodes a property
of the old fixture rather than a property of corporate-action handling, and it
would spuriously fail for any realistic random-walk series. The tests below
instead bound the ex-date return by the dispersion of the surrounding series.
"""

from __future__ import annotations

import statistics
from datetime import date

import polars as pl

from ledger.backtest.run_comparison import _build_observations, _generate_synthetic_data
from ledger.backtest.runner import run_comparison
from ledger.backtest.simulation import SimulationConfig
from ledger.backtest.strategy import StrategyConfig

SEC_IDS = ("SEC_AAPL_001", "SEC_MSFT_001", "SEC_NVDA_001")
START_DATE = date(2020, 1, 1)
END_DATE = date(2020, 12, 31)

# A single trading day cannot legitimately move a large-cap by more than this.
# A 4:1 split mishandled as a multiplication produces -75% (raw only) or
# -93.75% (doubled). 20% leaves generous headroom for a ~28% annual-vol asset
# while failing hard on any price-cliff defect.
MAX_ABS_DAILY_RETURN = 0.20


def _run(sec_ids: tuple[str, ...] = SEC_IDS) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    raw, preadjusted, splits = _generate_synthetic_data(sec_ids, START_DATE, END_DATE)
    observation_matrix = _build_observations(raw, sec_ids, START_DATE, END_DATE)
    comparison = run_comparison(
        observation_matrix=observation_matrix,
        raw_prices=raw,
        preadjusted_prices=preadjusted,
        splits=splits,
        strategy_config=StrategyConfig(top_n=len(sec_ids), rebalance_frequency="daily"),
        simulation_config=SimulationConfig(transaction_cost_bps=0.0),
        leaky_universe=("AAPL", "MSFT", "NVDA"),
    )
    return comparison, splits, raw


def _daily_returns(features: pl.DataFrame, sec_id: str) -> tuple[list[date], list[float]]:
    series = (
        features.filter(pl.col("sec_id") == sec_id)
        .sort("observation_timestamp")
        .select(["observation_timestamp", "adj_close"])
        .drop_nulls("adj_close")
    )
    timestamps = series["observation_timestamp"].to_list()
    prices = series["adj_close"].to_list()
    dates_ = [ts.date() for ts in timestamps]
    returns = [prices[i + 1] / prices[i] - 1.0 for i in range(len(prices) - 1)]
    return dates_, returns


def _assert_no_price_cliff(features: pl.DataFrame, label: str) -> None:
    for sec_id in features["sec_id"].unique().to_list():
        dates_, returns = _daily_returns(features, sec_id)
        offenders = [
            (d, r)
            for d, r in zip(dates_[1:], returns, strict=True)
            if abs(r) > MAX_ABS_DAILY_RETURN
        ]
        assert not offenders, (
            f"{label}/{sec_id}: price cliff detected at ex-date. "
            f"Returns exceeding +/-{MAX_ABS_DAILY_RETURN:.0%}: {offenders[:3]}"
        )


def test_corrected_pipeline_has_no_price_cliff_across_split() -> None:
    """The corrected pipeline must not produce an artificial jump at the split.

    Fails if the CAF is applied in the wrong direction (multiplying instead of
    dividing), which reintroduces a 16:1 cliff.
    """
    comparison, _splits, _raw = _run()
    _assert_no_price_cliff(comparison.corrected.features, "corrected")


def test_leaky_pipeline_has_no_price_cliff_across_split() -> None:
    """The leaky control uses vendor pre-adjusted prices and must be continuous.

    Vendor pre-adjustment rescales historical levels but preserves returns, so
    the control pipeline should not show a cliff either. This guards against a
    fixture that bakes the defect into the "control" arm.
    """
    comparison, _splits, _raw = _run()
    _assert_no_price_cliff(comparison.leaky.features, "leaky")


def test_returns_near_ex_date_are_not_outliers() -> None:
    """Returns in a window around the ex-date are statistically ordinary.

    Rather than demanding a positive return, bound the ex-date move by the
    dispersion of the rest of the series. A correctly adjusted split leaves the
    return drawn from the same distribution as every other day.

    The window matters. The point-in-time engine is knowledge-lagged: an
    observation at 21:05 sees the bar confirmed at 21:15 the *previous* day, so
    the return that straddles the ex-date surfaces one observation after it. A
    single-index check would therefore miss the defect entirely.
    """
    comparison, splits, _raw = _run()
    split_sec_id = splits["sec_id"][0]
    ex_date = splits["ex_date"][0]

    dates_, returns = _daily_returns(comparison.corrected.features, split_sec_id)
    ex_index = dates_.index(ex_date) - 1  # return *into* the ex-date

    window = list(range(max(0, ex_index - 2), min(len(returns), ex_index + 3)))
    others = [r for i, r in enumerate(returns) if i not in window]

    sigma = statistics.stdev(others)
    mean = statistics.mean(others)

    offenders = [
        (dates_[i + 1], r, (r - mean) / sigma)
        for i in window
        if abs((r := returns[i]) - mean) / sigma > 6.0
    ]
    assert not offenders, (
        f"Return(s) within +/-2 days of ex-date {ex_date} are statistical outliers: "
        f"{[(str(d), f'{r:+.4%}', f'{z:.1f} sigma') for d, r, z in offenders]}. "
        f"Series mean={mean:+.4%}, sigma={sigma:.4%}. "
        "A split must change price levels, not economic returns."
    )


def test_split_does_not_change_economic_returns_of_the_control() -> None:
    """Vendor pre-adjustment preserves returns; confirm it on the fixture.

    This is the check that keeps the demo honest. If the control arm's
    pre-adjusted series ever diverges in returns from the underlying path, the
    leaky-vs-corrected comparison is measuring a fixture artifact.
    """
    comparison, splits, _raw = _run()
    split_sec_id = splits["sec_id"][0]

    _leaky_dates, leaky_returns = _daily_returns(comparison.leaky.features, split_sec_id)
    _corr_dates, corr_returns = _daily_returns(comparison.corrected.features, split_sec_id)

    common = min(len(leaky_returns), len(corr_returns))
    diffs = [abs(leaky_returns[i] - corr_returns[i]) for i in range(common)]

    # The two arms differ only by the knowledge-time lag, so same-index returns
    # should agree closely. A large divergence means one arm is mis-scaled.
    assert max(diffs) < 0.05, (
        f"Leaky and corrected returns diverge by up to {max(diffs):.4%} at the same index. "
        "Pre-adjustment must preserve returns; a large gap indicates a scaling defect."
    )
