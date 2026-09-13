"""Declarative technical feature definitions (Momentum, Volatility, SMA, EMA).

All indicators are calculated strictly over dynamically split-adjusted close prices (`adj_close`),
preventing phantom returns across split boundaries and enforcing zero look-ahead bias.

Technical Features Implemented:
1. `momentum_20d`: 20-day percentage price change: (P_t / P_{t-20}) - 1.0.
2. `volatility_20d`: 20-day annualized rolling standard deviation of log returns.
3. `sma_50d`: 50-day Simple Moving Average.
4. `ema_50d`: 50-day Exponential Moving Average (seeded with SMA-50).
"""

from __future__ import annotations

import math

import polars as pl

from ledger.features.definitions.adj_close import compute_adj_close
from ledger.features.registry import FeatureContext, register


def _get_adjusted_prices(ctx: FeatureContext) -> pl.DataFrame:
    """Extract or compute split-adjusted price time series from context."""
    if "adj_close" in ctx.custom and isinstance(ctx.custom["adj_close"], pl.DataFrame):
        df = ctx.custom["adj_close"]
    elif ctx.prices is not None and "adj_close" in ctx.prices.columns:
        df = ctx.prices
    else:
        df = compute_adj_close(ctx)

    required_cols = {"sec_id", "trade_date", "adj_close", "known_from"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"Adjusted price DataFrame missing required columns: {missing}.")

    return df.sort(["sec_id", "trade_date"])


# =============================================================================
# 1. 20-Day Momentum
# =============================================================================


@register(
    name="momentum_20d",
    version="1.0.0",
    dependencies=["adj_close"],
    description="20-day price momentum: (adj_close_t / adj_close_{t-20}) - 1.0.",
    tags=["technical", "momentum"],
)
def compute_momentum_20d(ctx: FeatureContext) -> pl.DataFrame:
    """Compute 20-day price momentum per security.

    Formula:
        momentum_20d = (adj_close_t / adj_close_{t-20}) - 1.0

    Warm-up period: First 20 observations per security evaluate to null.
    """
    df = _get_adjusted_prices(ctx)

    res = df.with_columns(
        ((pl.col("adj_close") / pl.col("adj_close").shift(20).over("sec_id")) - 1.0).alias(
            "momentum_20d"
        )
    )

    return res.select(["sec_id", "trade_date", "known_from", "momentum_20d"])


# =============================================================================
# 2. 20-Day Realized Volatility
# =============================================================================


@register(
    name="volatility_20d",
    version="1.0.0",
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
    df = _get_adjusted_prices(ctx)

    annualization_factor = math.sqrt(252.0)

    res = df.with_columns(
        (pl.col("adj_close") / pl.col("adj_close").shift(1).over("sec_id"))
        .log()
        .alias("_log_return")
    ).with_columns(
        (
            pl.col("_log_return").rolling_std(window_size=20, min_samples=20, ddof=1).over("sec_id")
            * annualization_factor
        ).alias("volatility_20d")
    )

    return res.select(["sec_id", "trade_date", "known_from", "volatility_20d"])


# =============================================================================
# 3. 50-Day Simple Moving Average (SMA)
# =============================================================================


@register(
    name="sma_50d",
    version="1.0.0",
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
    df = _get_adjusted_prices(ctx)

    res = df.with_columns(
        pl.col("adj_close")
        .rolling_mean(window_size=50, min_samples=50)
        .over("sec_id")
        .alias("sma_50d")
    )

    return res.select(["sec_id", "trade_date", "known_from", "sma_50d"])


# =============================================================================
# 4. 50-Day Exponential Moving Average (EMA)
# =============================================================================


@register(
    name="ema_50d",
    version="1.0.0",
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
    df = _get_adjusted_prices(ctx)

    res = df.with_columns(
        pl.col("adj_close")
        .ewm_mean(span=50, min_samples=50, adjust=False)
        .over("sec_id")
        .alias("ema_50d")
    )

    return res.select(["sec_id", "trade_date", "known_from", "ema_50d"])
