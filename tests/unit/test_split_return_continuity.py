"""Regression tests for corporate-action (split) handling.

Why these tests exist
---------------------
An earlier demo fixture multiplied pre-split prices by the split ratio *and*
stored raw prices that were already 4x elevated, producing a spurious 16:1
price cliff at the split date. That cliff showed up as a ~-93% single-day
asset return and a ~-31% portfolio drawdown which was then mis-reported as
evidence of lookahead bias. It was a bug in the demo, not leakage.

Two different quantities
------------------------
A split changes price *levels* but not economic *returns*. Separating the two
is the whole point, and the two arms of this comparison sit on opposite sides
of that line:

* The **corrected** arm reduces a split only once the split is announced, so
  its level series legitimately jumps once, on the ex-date. That jump is
  information, not a defect: the decision genuinely predates the news.
* The **leaky** arm uses vendor pre-adjusted prices, whose levels are already
  retroactively rescaled and therefore continuous.

So "no cliff" is the wrong assertion for the corrected arm -- it would only be
satisfiable by reintroducing look-ahead. The assertions below instead pin down
three things that must hold regardless of basis:

1. The corrected level series has exactly one discontinuity, on the ex-date,
   in one security. (Stronger than "none": it also fails if the split is
   ignored entirely, or applied twice.)
2. Economic returns -- level returns corrected for splits effective that day --
   are ordinary on both sides of the ex-date.
3. The corrected arm's economic returns equal the control arm's level returns
   exactly. Same underlying path, different adjustment basis.

Invariant 3 is the strongest of the three: it ties the two arms together, so a
scaling defect anywhere in the fixture shows up immediately.

A monotone "all returns >= 0" assertion is deliberately avoided. It is only
satisfiable by a deterministic, noise-free price path; it encodes a property of
the old fixture rather than of corporate-action handling, and it would spuriously
fail for any realistic random-walk series.
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
# 20% leaves generous headroom for a ~28% annual-vol asset while failing hard
# on any price-cliff defect. A 4:1 split mishandled as a multiplication produces
# -75% (raw only) or -93.75% (doubled).
MAX_ABS_DAILY_RETURN = 0.20

# Cross-arm agreement tolerance on returns. The two arms are algebraically
# identical once each is reduced to an economic return, so this is a tight
# numerical-agreement bound, not a statistical one.
MAX_ARM_RETURN_DISAGREEMENT = 1e-6


def _run(sec_ids: tuple[str, ...] = SEC_IDS) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    raw, preadjusted, splits, _fundamentals = _generate_synthetic_data(
        sec_ids, START_DATE, END_DATE
    )
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


def _level_series(features: pl.DataFrame, sec_id: str) -> tuple[list[date], list[float]]:
    series = (
        features.filter(pl.col("sec_id") == sec_id)
        .sort("observation_timestamp")
        .select(["observation_timestamp", "adj_close"])
        .drop_nulls("adj_close")
    )
    timestamps = series["observation_timestamp"].to_list()
    prices = series["adj_close"].to_list()
    return [ts.date() for ts in timestamps], prices


def _level_returns(features: pl.DataFrame, sec_id: str) -> tuple[list[date], list[float]]:
    """Plain close-to-close returns of the level series as stored."""
    dates_, prices = _level_series(features, sec_id)
    returns = [prices[i + 1] / prices[i] - 1.0 for i in range(len(prices) - 1)]
    return dates_, returns


def _split_ratios_by_date(splits: pl.DataFrame) -> dict[str, dict[date, float]]:
    ratios: dict[str, dict[date, float]] = {}
    for row in splits.iter_rows(named=True):
        ratios.setdefault(row["sec_id"], {})[row["ex_date"]] = row["split_ratio"]
    return ratios


def _economic_returns(
    features: pl.DataFrame, sec_id: str, ratios: dict[str, dict[date, float]]
) -> tuple[list[date], list[float]]:
    """Level returns corrected for splits effective on the later observation.

    A holder's wealth is unchanged by a split: the share count multiplies by
    the ratio while the price divides. So the economic return must carry the
    same ratio, which cancels the level jump.
    """
    dates_, prices = _level_series(features, sec_id)
    sec_ratios = ratios.get(sec_id, {})
    returns = [
        (prices[i + 1] / prices[i]) * sec_ratios.get(dates_[i + 1], 1.0) - 1.0
        for i in range(len(prices) - 1)
    ]
    return dates_, returns


def test_corrected_level_series_jumps_only_at_the_ex_date() -> None:
    """The corrected arm must show exactly one level discontinuity.

    A split that had not been announced when a decision was made cannot appear
    in the levels that decision saw, so the point-in-time level series is
    expected to carry a single ~-75% jump on the ex-date. Requiring *exactly*
    one, in *exactly* one security, is strictly stronger than the old "no
    cliff" check -- it also fails if the feature ignores the split entirely or
    applies it twice, and it fails hard on a reversed-direction CAF.
    """
    comparison, splits, _raw = _run()
    expected_sec_id = splits["sec_id"][0]
    expected_ex_date = splits["ex_date"][0]

    observed: dict[str, list[date]] = {}
    for sec_id in comparison.corrected.features["sec_id"].unique().to_list():
        dates_, returns = _level_returns(comparison.corrected.features, sec_id)
        offenders = [
            d for d, r in zip(dates_[1:], returns, strict=True) if abs(r) > MAX_ABS_DAILY_RETURN
        ]
        if offenders:
            observed[sec_id] = offenders

    assert set(observed) == {expected_sec_id}, (
        f"Expected a level discontinuity in {expected_sec_id} only; "
        f"got {observed}. Non-split securities must never show one, and the "
        "split security must show one -- a symmetric level series means the "
        "point-in-time adjustment is not being applied."
    )
    assert observed[expected_sec_id] == [expected_ex_date], (
        f"Expected the sole discontinuity on ex-date {expected_ex_date}; "
        f"got {observed[expected_sec_id]}."
    )


def test_leaky_pipeline_has_no_price_cliff_across_split() -> None:
    """The control uses vendor pre-adjusted levels and must be continuous.

    Pre-adjustment rescales historical levels but preserves returns, so the
    control arm's level series shows no cliff. This guards against a fixture
    that bakes the defect into the "control" arm.
    """
    comparison, _splits, _raw = _run()
    for sec_id in comparison.leaky.features["sec_id"].unique().to_list():
        dates_, returns = _level_returns(comparison.leaky.features, sec_id)
        offenders = [
            (d, r)
            for d, r in zip(dates_[1:], returns, strict=True)
            if abs(r) > MAX_ABS_DAILY_RETURN
        ]
        assert not offenders, (
            f"leaky/{sec_id}: price cliff in the pre-adjusted control. "
            f"Returns exceeding +/-{MAX_ABS_DAILY_RETURN:.0%}: {offenders[:3]}"
        )


def test_economic_returns_are_continuous_across_the_ex_date() -> None:
    """Once reduced to economic returns, the ex-date is an ordinary day.

    Rather than demanding a positive return, bound the ex-date move by the
    dispersion of the rest of the series. If the point-in-time adjustment is
    correct, the level jump is fully explained by the split and nothing is
    left over.

    The window matters. The point-in-time engine is knowledge-lagged, so the
    return that straddles the ex-date can surface one observation after it. A
    single-index check would therefore miss a real defect.
    """
    comparison, splits, _raw = _run()
    split_sec_id = splits["sec_id"][0]
    ex_date = splits["ex_date"][0]
    ratios = _split_ratios_by_date(splits)

    dates_, returns = _economic_returns(comparison.corrected.features, split_sec_id, ratios)

    breaches = [
        (d, r) for d, r in zip(dates_[1:], returns, strict=True) if abs(r) > MAX_ABS_DAILY_RETURN
    ]
    assert not breaches, (
        f"Economic returns still breach +/-{MAX_ABS_DAILY_RETURN:.0%} on {split_sec_id}: "
        f"{[(str(d), f'{r:+.2%}') for d, r in breaches[:3]]}. The split ratio must "
        "fully cancel the level jump; a residue means the CAF is mis-scaled."
    )

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


def test_economic_returns_match_the_preadjusted_control() -> None:
    """The cross-arm invariant: same underlying path, different basis.

    The corrected arm carries raw point-in-time levels and is discontinuous at
    the ex-date; the control carries vendor pre-adjusted levels and is
    continuous. Reduced to economic returns the two are algebraically
    identical, so they must agree to numerical precision. This is the check
    that keeps the demo honest: any divergence means one arm is mis-scaled and
    the leaky-vs-corrected comparison is measuring a fixture artifact.
    """
    comparison, splits, _raw = _run()
    ratios = _split_ratios_by_date(splits)

    worst = 0.0
    for sec_id in comparison.corrected.features["sec_id"].unique().to_list():
        _corr_dates, corr = _economic_returns(comparison.corrected.features, sec_id, ratios)
        _leaky_dates, leaky = _level_returns(comparison.leaky.features, sec_id)
        common = min(len(corr), len(leaky))
        diffs = [abs(corr[i] - leaky[i]) for i in range(common)]
        if diffs:
            worst = max(worst, max(diffs))

    assert worst < MAX_ARM_RETURN_DISAGREEMENT, (
        f"Corrected economic returns diverge from the pre-adjusted control by up to "
        f"{worst:.6%}. Pre-adjustment must preserve returns; a gap indicates a "
        "scaling defect in one of the arms."
    )
