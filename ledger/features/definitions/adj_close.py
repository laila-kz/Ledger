"""Base feature definition for point-in-time split-adjusted close prices."""

from __future__ import annotations

import polars as pl

from ledger.features.registry import FeatureContext, register

_OUTPUT_COLUMNS = ["sec_id", "trade_date", "close", "adj_close", "known_from"]

_PRICE_COLUMNS = ["sec_id", "trade_date", "close", "known_from"]
_SPLIT_COLUMNS = ["sec_id", "ex_date", "split_ratio", "known_from"]


def resolve_prices(ctx: FeatureContext) -> pl.DataFrame:
    """Return the raw (unadjusted) price frame, from context or from the catalog."""
    prices = ctx.prices
    if prices is None and ctx.catalog is not None:
        if hasattr(ctx.catalog, "query"):
            prices = ctx.catalog.query("SELECT * FROM fact_market_ohlcv_raw")
        elif hasattr(ctx.catalog, "execute"):
            prices = ctx.catalog.execute("SELECT * FROM fact_market_ohlcv_raw").pl()

    if prices is None or prices.is_empty():
        raise ValueError(
            "FeatureContext must provide a 'prices' DataFrame or a 'catalog' connection."
        )

    return prices.select(_PRICE_COLUMNS)


def resolve_splits(ctx: FeatureContext) -> pl.DataFrame | None:
    """Return the split (corporate action) frame, from context or from the catalog."""
    splits = ctx.splits
    if splits is None and ctx.catalog is not None:
        if hasattr(ctx.catalog, "query"):
            splits = ctx.catalog.query(
                "SELECT * FROM fact_corporate_actions WHERE action_type = 'SPLIT'"
            )
        elif hasattr(ctx.catalog, "execute"):
            splits = ctx.catalog.execute(
                "SELECT * FROM fact_corporate_actions WHERE action_type = 'SPLIT'"
            ).pl()

    if splits is None or splits.is_empty():
        return None

    return splits.select(_SPLIT_COLUMNS)


@register(
    name="adj_close",
    version="2.0.0",
    dependencies=[],
    description="Point-in-time split-adjusted close price using cumulative adjustment factors.",
    tags=["base", "pricing", "caf"],
)
def compute_adj_close(ctx: FeatureContext) -> pl.DataFrame:
    """Compute split-adjusted prices that are correct *as of the moment each price was known*.

    Point-in-time contract
    ----------------------
    A split is a future fact for every price dated before its ex-date. So when a
    price is confirmed at ``known_from`` the adjustment may only use splits that
    were already announced at that instant::

        adj_close(t) = raw_close(t) * PROD(1 / split_ratio_k)
                       for splits k with t < ex_date_k
                                     and known_from_k <= known_from(t)

    Both conditions matter, and they do different jobs:

    * ``t < ex_date_k``  - the split must actually post-date this price.
    * ``known_from_k <= known_from(t)`` - the split must not be a *future* fact.

    Omitting the second condition is look-ahead bias: the price was genuinely
    known, but the adjustment folded in a split that had not happened yet. That
    is exactly what the previous implementation did by evaluating every row
    against a single end-of-series ``known_from.max()`` timestamp, which made a
    "point-in-time" series byte-identical to a statically vendor-adjusted one.

    Note this makes the *level* series discontinuous across an ex-date: a price
    confirmed before the split is never retroactively rescaled. Return-based
    indicators must therefore apply their own window correction -- see
    ``ledger/features/definitions/technical.py``.

    One row is emitted per price, stamped with that price's own ``known_from``,
    which keeps the downstream backward ASOF join deterministic.
    """
    prices = resolve_prices(ctx).unique(subset=["sec_id", "trade_date"], keep="last")
    splits = resolve_splits(ctx)

    # No corporate actions in scope: the adjustment is identically 1.0.
    if splits is None:
        return (
            prices.with_columns(pl.col("close").alias("adj_close"))
            .sort(["sec_id", "trade_date"])
            .select(_OUTPUT_COLUMNS)
        )

    expanded = (
        prices.sort(["sec_id", "trade_date"])
        .join(
            splits.rename({"known_from": "_split_known_from"}),
            on="sec_id",
            how="left",
        )
        .with_columns(
            pl.when(
                pl.col("ex_date").is_not_null()
                & (pl.col("trade_date") < pl.col("ex_date"))
                & (pl.col("_split_known_from") <= pl.col("known_from"))
            )
            .then(1.0 / pl.col("split_ratio"))
            .otherwise(pl.lit(None, dtype=pl.Float64))
            .alias("_qualifying_ratio")
        )
    )

    adjusted = (
        expanded.group_by(_PRICE_COLUMNS, maintain_order=True)
        .agg(pl.col("_qualifying_ratio").drop_nulls().product().alias("caf"))
        .with_columns(pl.col("caf").fill_null(1.0))
        .with_columns((pl.col("close") * pl.col("caf")).alias("adj_close"))
        .select(_OUTPUT_COLUMNS)
        .sort(["sec_id", "trade_date"])
    )

    return adjusted
