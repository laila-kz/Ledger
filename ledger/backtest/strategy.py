"""Reference cross-sectional momentum strategy.

The strategy is deliberately a pure Polars transformation. It consumes joined
point-in-time feature rows and returns the stable three-column weight contract
used by the vectorized portfolio runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import polars as pl


@dataclass(frozen=True)
class StrategyConfig:
    """Parameters for the reference long-only momentum strategy."""

    top_n: int = 10
    rebalance_frequency: Literal["daily", "weekly"] = "daily"
    weekly_rebalance_day: int = 0

    def __post_init__(self) -> None:
        if self.top_n < 1:
            raise ValueError("top_n must be at least 1.")
        if self.rebalance_frequency not in {"daily", "weekly"}:
            raise ValueError("rebalance_frequency must be 'daily' or 'weekly'.")
        if self.weekly_rebalance_day not in range(7):
            raise ValueError("weekly_rebalance_day must be between 0 (Monday) and 6 (Sunday).")


def generate_target_weights(
    features: pl.DataFrame,
    config: StrategyConfig | None = None,
    *,
    price_column: str | None = None,
) -> pl.DataFrame:
    """Generate cross-sectional target weights for each observation timestamp.

    The input is the joined point-in-time feature frame produced by the feature
    engine. Required columns are ``observation_timestamp``, ``sec_id``,
    ``momentum_20d``, ``volatility_20d``, and ``sma_50d``. The trend filter
    compares ``sma_50d`` with ``price_column``; when omitted, ``adj_close`` or
    ``close`` is selected. Rows that fail the warm-up or trend/volatility checks
    receive zero weight.

    Weights are calculated independently for each date: the highest-momentum
    eligible securities up to ``top_n`` are selected, then weighted by inverse
    volatility and normalized so the day's investable weights sum to one.
    """
    strategy_config = config or StrategyConfig()
    required = {
        "sec_id",
        "observation_timestamp",
        "momentum_20d",
        "volatility_20d",
        "sma_50d",
    }
    missing = sorted(required - set(features.columns))
    if missing:
        raise ValueError(f"Strategy features missing required columns: {missing}")

    if price_column is None:
        price_column = "adj_close" if "adj_close" in features.columns else "close"
    if price_column not in features.columns:
        raise ValueError(
            f"Trend price column '{price_column}' is missing; provide `adj_close` or `close`."
        )

    if features.is_empty():
        return features.select(["observation_timestamp", "sec_id"]).with_columns(
            pl.lit(0.0).alias("weight")
        )

    result = features.with_columns(
        pl.lit(0.0).cast(pl.Float64).alias("weight"),
        pl.col("observation_timestamp").dt.weekday().alias("_weekday"),
    )
    eligible = result.filter(
        pl.col("momentum_20d").is_not_null()
        & pl.col("volatility_20d").is_not_null()
        & (pl.col("volatility_20d") > 0.0)
        & pl.col("sma_50d").is_not_null()
        & pl.col(price_column).is_not_null()
        & (pl.col(price_column) > pl.col("sma_50d"))
    )

    if strategy_config.rebalance_frequency == "weekly":
        eligible = eligible.filter(pl.col("_weekday") == strategy_config.weekly_rebalance_day + 1)

    ranked = (
        eligible.with_columns(
            pl.col("momentum_20d")
            .rank(method="ordinal", descending=True)
            .over("observation_timestamp")
            .alias("_momentum_rank")
        )
        .filter(pl.col("_momentum_rank") <= strategy_config.top_n)
        .with_columns((1.0 / pl.col("volatility_20d")).alias("_inverse_volatility"))
        .with_columns(
            (
                pl.col("_inverse_volatility")
                / pl.col("_inverse_volatility").sum().over("observation_timestamp")
            ).alias("_selected_weight")
        )
        .select(["observation_timestamp", "sec_id", "_selected_weight"])
    )

    result = result.join(ranked, on=["sec_id", "observation_timestamp"], how="left").with_columns(
        pl.coalesce([pl.col("_selected_weight"), pl.col("weight")]).alias("weight")
    )

    if strategy_config.rebalance_frequency == "weekly":
        result = (
            result.sort(["sec_id", "observation_timestamp"])
            .with_columns(
                pl.col("weight").replace(0.0, None).forward_fill().over("sec_id").fill_null(0.0)
            )
            .filter(pl.col("_weekday") == strategy_config.weekly_rebalance_day + 1)
        )

    return result.select(["observation_timestamp", "sec_id", "weight"])


__all__ = ["StrategyConfig", "generate_target_weights"]
