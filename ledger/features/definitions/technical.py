"""Declarative technical feature definitions (Momentum, Volatility, SMA, EMA).

Point-in-time split handling
---------------------------
Every indicator here is a *window* statistic, so a split inside the window has
to be neutralised using only the splits that were already announced when the
window's anchor price was confirmed.

The previous implementation sidestepped this by rolling over ``adj_close``,
which applied a single end-of-series CAF to the whole history. That made the
indicators continuous across splits, but only by discounting the future: the
adjustment used to cancel the ex-date jump was itself not knowable at the time.

Here the series is rebuilt *per knowledge state* instead. The set of splits
known at a price is always a prefix of the splits ordered by ``known_from``, so
the entire history partitions into at most ``n_splits + 1`` groups. Each group
is corrected with exactly the splits its members knew about, and the rolling
window is computed over the full series *before* selecting that group's rows --
so warm-up periods are preserved rather than truncated by the grouping.

Technical Features Implemented:
1. `momentum_20d`: 20-day percentage price change: (P_t / P_{t-20}) - 1.0.
2. `volatility_20d`: 20-day annualized rolling standard deviation of log returns.
3. `sma_50d`: 50-day Simple Moving Average.
4. `ema_50d`: 50-day Exponential Moving Average (seeded with SMA-50).
"""

from __future__ import annotations

import math

import polars as pl

from ledger.features.definitions.adj_close import resolve_prices, resolve_splits
from ledger.features.registry import FeatureContext, register

_IDG_COLUMNS = ["sec_id", "trade_date", "known_from"]
_CACHE_KEY = "_pit_window_indicators"


def _pit_window_indicators(ctx: FeatureContext) -> pl.DataFrame:
    """Build every rolling indicator, corrected per knowledge state.

    Returns one row per price [sec_id, trade_date, known_from] carrying
    momentum_20d, volatility_20d, sma_50d and ema_50d.
    """
    cached = ctx.custom.get(_CACHE_KEY)
    if isinstance(cached, pl.DataFrame):
        return cached

    prices = resolve_prices(ctx).unique(subset=["sec_id", "trade_date"], keep="last")
    splits = resolve_splits(ctx)

    prices = prices.sort(["sec_id", "trade_date"])

    if splits is None:
        prices = prices.with_columns(pl.lit(0, dtype=pl.Int64).alias("_known_splits"))

    else:
        ordered = (
            splits.sort(["sec_id", "known_from"])
            .with_columns(pl.col("known_from").cum_count().over("sec_id").alias("_split_idx"))
            .select(["sec_id", "known_from", "_split_idx"])
        )
        # Number of splits announced at or before each price's own confirmation.
        # join_asof on the announcement timestamp gives the position of the last
        # announced split; none announced yet means zero.
        prices = (
            prices.sort(["sec_id", "known_from"])
            .join_asof(
                ordered,
                left_on="known_from",
                right_on="known_from",
                by_left="sec_id",
                by_right="sec_id",
                strategy="backward",
            )
            .with_columns(
                pl.when(pl.col("_split_idx").is_null())
                .then(0)
                .otherwise(pl.col("_split_idx") + 1)
                .cast(pl.Int64)
                .alias("_known_splits")
            )
            .sort(["sec_id", "trade_date"])
        )

    frames: list[pl.DataFrame] = []
    for k in sorted(prices["_known_splits"].unique().to_list()):
        member_ids = prices.filter(pl.col("_known_splits") == k).select("sec_id").unique()["sec_id"]
        subset = prices.filter(pl.col("sec_id").is_in(member_ids))

        if splits is None:
            corrected = subset.with_columns(pl.col("close").alias("_adj"))
        else:
            applicable = (
                splits.sort(["sec_id", "known_from"])
                .with_columns(pl.col("known_from").cum_count().over("sec_id").alias("_split_idx"))
                .filter((pl.col("_split_idx") < k) & pl.col("sec_id").is_in(member_ids))
                .select(["sec_id", "ex_date", "split_ratio"])
            )
            corrected = (
                subset.join(applicable, on="sec_id", how="left")
                .with_columns(
                    pl.when(
                        pl.col("ex_date").is_not_null() & (pl.col("trade_date") < pl.col("ex_date"))
                    )
                    .then(pl.col("close") / pl.col("split_ratio"))
                    .otherwise(pl.col("close"))
                    .alias("_adj")
                )
                .select(_IDG_COLUMNS + ["close", "_adj", "_known_splits"])
            )

        frames.append(
            corrected.sort(["sec_id", "trade_date"])
            .with_columns(pl.col("_adj").log().alias("_log_adj"))
            .with_columns(
                (
                    (pl.col("_log_adj") - pl.col("_log_adj").shift(20).over("sec_id")).exp() - 1.0
                ).alias("momentum_20d")
            )
            .with_columns(
                (pl.col("_log_adj") - pl.col("_log_adj").shift(1).over("sec_id")).alias("_log_ret")
            )
            .with_columns(
                (
                    pl.col("_log_ret")
                    .rolling_std(window_size=20, min_samples=20, ddof=1)
                    .over("sec_id")
                    * math.sqrt(252.0)
                ).alias("volatility_20d"),
                pl.col("_adj")
                .rolling_mean(window_size=50, min_samples=50)
                .over("sec_id")
                .alias("sma_50d"),
                pl.col("_adj")
                .ewm_mean(span=50, min_samples=50, adjust=False)
                .over("sec_id")
                .alias("ema_50d"),
            )
            .filter(pl.col("_known_splits") == k)
        )

    result = pl.concat(frames).sort(["sec_id", "trade_date"])
    ctx.custom[_CACHE_KEY] = result
    return result


# =============================================================================
# 1. 20-Day Momentum
# =============================================================================


@register(
    name="momentum_20d",
    version="2.0.0",
    dependencies=["adj_close"],
    description="20-day price momentum: (adj_close_t / adj_close_{t-20}) - 1.0.",
    tags=["technical", "momentum"],
)
def compute_momentum_20d(ctx: FeatureContext) -> pl.DataFrame:
    """Compute 20-day price momentum per security.

    Formula:
        momentum_20d = (adj_close_t / adj_close_{t-20}) - 1.0

    where each ``adj_close`` is point-in-time adjusted using only the splits
    known when the anchor price was confirmed.

    Warm-up period: First 20 observations per security evaluate to null.
    """
    ind = _pit_window_indicators(ctx)
    return ind.select(_IDG_COLUMNS + ["momentum_20d"])


# =============================================================================
# 2. 20-Day Realized Volatility
# =============================================================================


@register(
    name="volatility_20d",
    version="2.0.0",
    dependencies=["adj_close"],
    description="20-day annualized rolling standard deviation of daily log returns.",
    tags=["technical", "volatility"],
)
def compute_volatility_20d(ctx: FeatureContext) -> pl.DataFrame:
    """Compute 20-day annualized realized volatility per security.

    Formula:
        r_t = ln(adj_close_t / adj_close_{t-1})
        volatility_20d = std_sample(r_{t-19...t}) * sqrt(252)

    Warm-up period: First 20 observations per security evaluate to null.
    """
    ind = _pit_window_indicators(ctx)
    return ind.select(_IDG_COLUMNS + ["volatility_20d"])


# =============================================================================
# 3. 50-Day Simple Moving Average (SMA)
# =============================================================================


@register(
    name="sma_50d",
    version="2.0.0",
    dependencies=["adj_close"],
    description="50-day Simple Moving Average (SMA) of split-adjusted close prices.",
    tags=["technical", "trend", "moving_average"],
)
def compute_sma_50d(ctx: FeatureContext) -> pl.DataFrame:
    """Compute 50-day Simple Moving Average per security.

    Formula:
        sma_50d = (1 / 50) * sum_{i=0}^{49} adj_close_{t-i}

    Warm-up period: First 49 observations per security evaluate to null.
    """
    ind = _pit_window_indicators(ctx)
    return ind.select(_IDG_COLUMNS + ["sma_50d"])


# =============================================================================
# 4. 50-Day Exponential Moving Average (EMA)
# =============================================================================


@register(
    name="ema_50d",
    version="2.0.0",
    dependencies=["adj_close"],
    description="50-day Exponential Moving Average (EMA) of split-adjusted close prices.",
    tags=["technical", "trend", "moving_average"],
)
def compute_ema_50d(ctx: FeatureContext) -> pl.DataFrame:
    """Compute 50-day Exponential Moving Average per security.

    Formula:
        alpha = 2 / (50 + 1) = 2 / 51
        ema_t = alpha * adj_close_t + (1 - alpha) * ema_{t-1}

    Warm-up period: First 49 observations evaluate to null.

    Seeding Convention:
        Uses Polars `ewm_mean(span=50, min_samples=50, adjust=False)`. Recursion
        initiates from P_0 and nulls the first 49 bars. Differs from TA-Lib/Bloomberg
        SMA_50-initialization by < 0.4% in initial bars, decaying asymptotically to
        zero over longer histories (documented in ADR 007).
    """
    ind = _pit_window_indicators(ctx)
    return ind.select(_IDG_COLUMNS + ["ema_50d"])
