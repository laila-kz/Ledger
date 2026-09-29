"""Controlled-experiment guard: with no leak injected, the two pipelines must agree.

The demo compares a deliberately leaky control against the point-in-time
pipeline. That comparison is only interpretable if it is a *controlled*
experiment: the leaky arm must be handed the same data under a different
availability rule, and nothing else may differ.

This module asserts that control directly. It runs ``run_comparison`` with no
fundamentals views at all -- so there is no injected leak -- and requires the two
arms to agree on the *economic* features and on target weights, with any level
divergence provably attributable to the split adjustment basis alone.

Note that adjusted *levels* are expected to differ between the arms, and must
not be asserted equal. The corrected arm reduces a split only once announced,
so its level series is deliberately discontinuous across the ex-date; the leaky
arm carries vendor pre-adjusted levels, which are continuous. Requiring the two
level series to match would only be satisfiable by reinstating look-ahead in the
corrected arm. What must hold instead is that the divergence is exactly the
split ratio, confined to the split security, and that every economic feature --
what the strategy actually ranks on -- agrees.

The historical regression this guards against: observations were stamped at
21:05 UTC while bars were confirmed at 21:15 UTC, so the point-in-time arm never
saw the current day's bar and silently traded a one-day-lagged price series.
That affected every security on every day, and it -- not any real leak -- was
producing the headline divergence.
"""

from datetime import date, datetime, time, timezone

import polars as pl

from ledger.backtest.runner import ComparisonResult, run_comparison
from ledger.backtest.simulation import SimulationConfig
from ledger.backtest.strategy import StrategyConfig
from ledger.backtest.synthetic import (
    _CONFIRMATION_TIME,
    OBSERVATION_TIME,
    generate_synthetic_data,
)

UTC = timezone.utc
START = date(2020, 1, 1)
END = date(2023, 12, 31)

# The arms reach their economic features via different price frames, so
# agreement is to floating-point rounding. The timing confound this module
# guards against moved these by ~46, so the bound has enormous headroom.
MAX_ECONOMIC_FEATURE_DISAGREEMENT = 1e-12
SEC_IDS = (
    "SEC_AAPL_001",
    "SEC_MSFT_001",
    "SEC_NVDA_001",
    "SEC_META_001",
    "SEC_GOOGL_001",
)


def _observations(raw: pl.DataFrame, observation_time: time) -> pl.DataFrame:
    dates = raw.select("trade_date").unique().sort("trade_date")["trade_date"].to_list()
    stamps = [datetime.combine(d, observation_time, tzinfo=UTC) for d in dates]
    return pl.DataFrame(
        {
            "sec_id": [s for s in SEC_IDS for _ in stamps],
            "observation_timestamp": stamps * len(SEC_IDS),
        }
    )


def _compare(observation_time: time) -> ComparisonResult:
    raw, preadj, splits, _fundamentals = generate_synthetic_data(SEC_IDS, START, END)
    return run_comparison(
        observation_matrix=_observations(raw, observation_time),
        raw_prices=raw,
        preadjusted_prices=preadj,
        splits=splits,
        strategy_config=StrategyConfig(top_n=3),
        simulation_config=SimulationConfig(transaction_cost_bps=5.0),
        leaky_universe=SEC_IDS,
    )


def test_observation_lands_after_bar_confirmation() -> None:
    """The observation must not precede the bar's ``known_from`` stamp.

    Otherwise the point-in-time arm cannot see the bar it is supposed to be
    reacting to, and the whole comparison is measuring a one-day lag.
    """
    assert OBSERVATION_TIME > _CONFIRMATION_TIME, (
        f"OBSERVATION_TIME={OBSERVATION_TIME} must be later than "
        f"_CONFIRMATION_TIME={_CONFIRMATION_TIME}, otherwise the point-in-time "
        f"arm trades on a lagged price series."
    )


def _split_facts() -> tuple[str, date, float]:
    """Return (split security, ex_date, ratio) for the synthetic fixture."""
    _raw, _preadj, splits, _fund = generate_synthetic_data(SEC_IDS, START, END)
    return splits["sec_id"][0], splits["ex_date"][0], splits["split_ratio"][0]


def test_arms_differ_only_by_split_basis_when_no_leak_injected() -> None:
    """With no leak injected, the arms must differ *only* in adjustment basis.

    The corrected arm reduces a split only once it is announced, so its level
    series is deliberately discontinuous across the ex-date. The control carries
    vendor pre-adjusted levels, which are continuous by construction. The two
    bases are not expected to agree, and demanding that they do would only be
    satisfiable by reinstating look-ahead in the corrected arm.

    What must hold is narrower and stricter:

    * securities with no split are bit-identical across arms;
    * where the arms do differ, the ratio is exactly the split ratio -- not an
      approximation of it, which would indicate a mis-scaled CAF;
    * every *economic* feature, which is what the strategy actually trades on,
      agrees exactly.

    A timing confound cannot survive this: it perturbs observations rather
    than adjustment, so it would break the economic-feature agreement and
    leave a ratio that is not the split ratio.
    """
    comparison = _compare(OBSERVATION_TIME)
    split_sec_id, _ex_date, ratio = _split_facts()

    joined = comparison.leaky.features.select(
        ["observation_timestamp", "sec_id", "adj_close", "momentum_20d", "volatility_20d"]
    ).join(
        comparison.corrected.features.select(
            [
                "observation_timestamp",
                "sec_id",
                "adj_close",
                "momentum_20d",
                "volatility_20d",
            ]
        ),
        on=["observation_timestamp", "sec_id"],
        how="inner",
        suffix="_corrected",
    )
    assert joined.height > 0, "no overlapping feature rows to compare"

    # Economic features must be identical: these are the split-corrected,
    # basis-independent quantities the strategy ranks on. The arms reach them
    # via different price frames, so agreement is to floating-point rounding
    # (1 ULP); the historical timing confound moved this by ~46.
    for feature in ("momentum_20d", "volatility_20d"):
        worst = (joined[feature] - joined[f"{feature}_corrected"]).abs().max()
        assert isinstance(worst, float), (
            f"{feature} difference must be a float, got {type(worst).__name__}"
        )
        assert worst <= MAX_ECONOMIC_FEATURE_DISAGREEMENT, (
            f"{feature} differs by up to {worst} across arms with no leak injected. "
            "Economic features are split-corrected and must be basis-independent; "
            "a difference here is an observation-timing confound, not a result."
        )

    # Level series may differ, but only by exactly the split ratio.
    joined = joined.with_columns(
        (pl.col("adj_close_corrected") / pl.col("adj_close")).alias("_level_ratio")
    )
    unadjusted = joined.filter(pl.col("_level_ratio").sub(1.0).abs() > 1e-9)
    assert set(unadjusted["sec_id"].unique().to_list()) <= {split_sec_id}, (
        f"Only {split_sec_id} splits; these securities also diverged: "
        f"{sorted(set(unadjusted['sec_id'].unique().to_list()))}. A non-split "
        "security must be bit-identical across arms."
    )
    if unadjusted.height:
        worst_ratio_error = (unadjusted["_level_ratio"] - ratio).abs().max()
        assert isinstance(worst_ratio_error, float), (
            f"level-ratio error must be a float, got {type(worst_ratio_error).__name__}"
        )
        assert worst_ratio_error < 1e-9, (
            f"Level ratio between arms departs from the split ratio {ratio} by "
            f"{worst_ratio_error}. The arms must differ by exactly one corporate "
            "action, which would fail for a reversed-direction or double CAF."
        )


def test_arms_agree_on_weights_when_no_leak_injected() -> None:
    """With no leak injected, target weights must be identical across arms."""
    comparison = _compare(OBSERVATION_TIME)
    leaky = comparison.leaky.weights.sort(["observation_timestamp", "sec_id"])
    corrected = comparison.corrected.weights.sort(["observation_timestamp", "sec_id"])
    joined = leaky.join(
        corrected, on=["observation_timestamp", "sec_id"], how="full", suffix="_corrected"
    )
    differing = joined.filter((pl.col("weight") - pl.col("weight_corrected")).abs() > 1e-12).height
    assert differing == 0, (
        f"{differing}/{joined.height} target-weight rows differ between arms with "
        f"no leak injected. A controlled experiment must show zero divergence."
    )


def test_pre_confirmation_observation_is_detected_as_confound() -> None:
    """Pin the historical defect: a pre-confirmation observation *is* a confound.

    This test deliberately regresses the observation timestamp to before the bar
    confirmation and asserts that the arms then disagree. It is the executable
    proof that the zero-difference result above is caused by the observation
    timing and is not an artifact of the assertion being trivially true.
    """
    from datetime import time as _time

    before_confirmation = _time(21, 5)
    comparison = _compare(before_confirmation)
    leaky = comparison.leaky.weights.sort(["observation_timestamp", "sec_id"])
    corrected = comparison.corrected.weights.sort(["observation_timestamp", "sec_id"])
    joined = leaky.join(
        corrected, on=["observation_timestamp", "sec_id"], how="full", suffix="_corrected"
    )
    differing = joined.filter((pl.col("weight") - pl.col("weight_corrected")).abs() > 1e-12).height
    assert differing > 0, (
        "Expected the pre-confirmation observation to produce divergent weights "
        "(the historical confound). If this is 0 the guard test above is vacuous."
    )
