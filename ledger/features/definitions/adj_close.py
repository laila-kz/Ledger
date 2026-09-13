"""Base feature definition for dynamically split-adjusted close prices."""

from __future__ import annotations

import polars as pl

from ledger.features.caf import compute_caf_matrix
from ledger.features.registry import FeatureContext, register


@register(
    name="adj_close",
    version="1.0.0",
    dependencies=[],
    description="Dynamically split-adjusted close price using cumulative adjustment factors (CAF).",
    tags=["base", "pricing", "caf"],
)
def compute_adj_close(ctx: FeatureContext) -> pl.DataFrame:
    """Compute dynamically split-adjusted prices for all securities in context.

    Args:
        ctx: FeatureContext containing `prices` and optional `splits` (or a `catalog`).

    Returns:
        Polars DataFrame containing [sec_id, trade_date, close, adj_close, known_from].
    """
    prices_df = ctx.prices
    splits_df = ctx.splits

    if prices_df is None and ctx.catalog is not None:
        if hasattr(ctx.catalog, "query"):
            prices_df = ctx.catalog.query("SELECT * FROM fact_market_ohlcv_raw")
        elif hasattr(ctx.catalog, "execute"):
            prices_df = ctx.catalog.execute("SELECT * FROM fact_market_ohlcv_raw").pl()

    if splits_df is None and ctx.catalog is not None:
        if hasattr(ctx.catalog, "query"):
            splits_df = ctx.catalog.query(
                "SELECT * FROM fact_corporate_actions WHERE action_type = 'SPLIT'"
            )
        elif hasattr(ctx.catalog, "execute"):
            splits_df = ctx.catalog.execute(
                "SELECT * FROM fact_corporate_actions WHERE action_type = 'SPLIT'"
            ).pl()

    if prices_df is None or prices_df.is_empty():
        raise ValueError(
            "FeatureContext must provide 'prices' DataFrame or a 'catalog' connection."
        )

    output_cols = ["sec_id", "trade_date", "close", "adj_close", "known_from"]

    # If splits_df is None or empty, return prices with adj_close == close
    if splits_df is None or splits_df.is_empty():
        res = prices_df.with_columns(pl.col("close").alias("adj_close"))
        avail = [c for c in output_cols if c in res.columns]
        extra = [c for c in res.columns if c not in avail]
        return res.select([*avail, *extra])

    # Determine evaluation observation points: latest known_from per security
    obs_df = prices_df.group_by("sec_id").agg(
        pl.col("known_from").max().alias("observation_timestamp")
    )

    # Compute dynamic cumulative adjustment factor
    adjusted_df = compute_caf_matrix(
        prices_df=prices_df,
        splits_df=splits_df,
        observation_df=obs_df,
    )

    # Re-attach original known_from from prices_df
    res = adjusted_df.join(
        prices_df.select(["sec_id", "trade_date", "known_from"]),
        on=["sec_id", "trade_date"],
        how="inner",
    ).sort(["sec_id", "trade_date"])

    output_cols = ["sec_id", "trade_date", "close", "adj_close", "known_from"]
    return res.select(output_cols)
