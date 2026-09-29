"""Vectorized daily portfolio return simulation."""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl


@dataclass(frozen=True)
class SimulationConfig:
    """Parameters shared by both comparison pipelines."""

    transaction_cost_bps: float = 5.0
    price_column: str = "price"
    initial_capital: float = 1.0

    def __post_init__(self) -> None:
        if self.transaction_cost_bps < 0.0:
            raise ValueError("transaction_cost_bps must be non-negative.")
        if not self.price_column:
            raise ValueError("price_column must not be empty.")
        if self.initial_capital <= 0.0:
            raise ValueError("initial_capital must be greater than zero.")


def simulate_portfolio(
    weights: pl.DataFrame,
    prices: pl.DataFrame,
    config: SimulationConfig | None = None,
    splits: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Simulate close-to-close returns from target weights.

    ``weights`` must contain ``observation_timestamp``, ``sec_id``, and
    ``weight``. ``prices`` must contain the same keys and ``config.price_column``.
    Target weights are applied for the interval starting at each observation.
    Turnover uses the prior interval's price-drifted holdings, not the prior
    target weights.

    Corporate actions
    -----------------
    ``splits`` is optional and means "corporate actions this price series has
    *not* already absorbed". Leave it out for a continuously pre-adjusted
    series such as a vendor ``auto_adjust`` feed, whose levels already step-free
    pass through an ex-date; supplying ``splits`` there would count the same
    split twice and inflate returns.

    A split is economically neutral to a holder: the share count multiplies by
    the ratio while the price divides, so total wealth is unchanged on the
    ex-date. Reading returns straight off a price *level* series therefore
    books a spurious loss equal to ``1 - 1/ratio`` for any series whose levels
    are not continuously pre-adjusted.

    That matters here precisely because the point-in-time arm is *not*
    continuously pre-adjusted -- it cannot be, since pre-adjusting a level
    series means folding in a split the decision predates. So the split ratio
    is applied to the return on the ex-date, which cancels the level jump and
    yields the holder's actual economic return.

    Only splits effective on the later observation are applied, and only when
    ``known_from <= next_timestamp``: the ratio must be knowable when the
    return is realised, so this introduces no look-ahead.

    Returns one row for every interval with a following observation timestamp:
    ``gross_return``, ``turnover``, ``cost``, ``net_return``, and
    cumulative ``equity``.
    """
    simulation_config = config or SimulationConfig()
    _require_columns(weights, {"observation_timestamp", "sec_id", "weight"}, "weights")
    _require_columns(
        prices,
        {"observation_timestamp", "sec_id", simulation_config.price_column},
        "prices",
    )

    if weights.is_empty() or prices.is_empty():
        return _empty_simulation()

    weight_keys = ["observation_timestamp", "sec_id"]
    if weights.select(weight_keys).is_duplicated().any():
        raise ValueError("weights must contain at most one row per observation and security.")
    if prices.select(weight_keys).is_duplicated().any():
        raise ValueError("prices must contain at most one row per observation and security.")

    timestamps = (
        weights.select("observation_timestamp")
        .unique()
        .sort("observation_timestamp")
        .with_columns(pl.col("observation_timestamp").shift(-1).alias("next_timestamp"))
        .drop_nulls("next_timestamp")
    )

    period_keys = ["observation_timestamp", "next_timestamp", "sec_id"]

    period_prices = (
        prices.join(timestamps, on="observation_timestamp", how="inner")
        .join(
            prices.select(
                [
                    "observation_timestamp",
                    "sec_id",
                    pl.col(simulation_config.price_column).alias("next_price"),
                ]
            ),
            left_on=["next_timestamp", "sec_id"],
            right_on=["observation_timestamp", "sec_id"],
            how="inner",
        )
        .rename({simulation_config.price_column: "current_price"})
        .select(period_keys + ["current_price", "next_price"])
    )

    if splits is not None and not splits.is_empty():
        # De-duplicate before aggregating. A split is identified by
        # (sec_id, ex_date, split_ratio); an append-only catalog that has been
        # re-ingested holds several byte-identical copies of the same event.
        # Multiplying across those copies would compound one 4:1 split into
        # 4**8 = 65,536 and manufacture a ~6,500% return, so the ratio must be
        # collapsed to one row per event before it is applied.
        split_events = splits.select(["sec_id", "ex_date", "split_ratio", "known_from"]).unique(
            subset=["sec_id", "ex_date", "split_ratio"], keep="first"
        )
        # A split belongs to an interval when it goes effective on that
        # interval's later observation *and* was already known by then.
        period_prices = (
            period_prices.join(split_events, on="sec_id", how="left")
            .with_columns(
                pl.when(
                    (pl.col("ex_date") == pl.col("next_timestamp").dt.date())
                    & (pl.col("known_from") <= pl.col("next_timestamp"))
                )
                .then(pl.col("split_ratio"))
                .otherwise(pl.lit(None, dtype=pl.Float64))
                .alias("_qualifying_ratio")
            )
            .group_by(period_keys, maintain_order=True)
            .agg(
                pl.col("current_price").first(),
                pl.col("next_price").first(),
                pl.col("_qualifying_ratio").drop_nulls().product().alias("split_ratio"),
            )
        )
    else:
        period_prices = period_prices.with_columns(pl.lit(1.0).alias("split_ratio"))

    period_prices = period_prices.with_columns(
        ((pl.col("next_price") / pl.col("current_price")) * pl.col("split_ratio") - 1.0).alias(
            "asset_return"
        )
    ).select(period_keys + ["current_price", "next_price", "split_ratio", "asset_return"])

    period_weights = weights.join(
        period_prices, on=["observation_timestamp", "sec_id"], how="inner"
    )
    gross_returns = period_weights.group_by("observation_timestamp").agg(
        (pl.col("weight") * pl.col("asset_return")).sum().alias("gross_return")
    )

    drifted = period_weights.with_columns(
        (
            pl.col("weight")
            * (1.0 + pl.col("asset_return"))
            / (
                1.0
                + pl.col("weight").mul(pl.col("asset_return")).sum().over("observation_timestamp")
            )
        ).alias("drifted_weight")
    ).select(["next_timestamp", "sec_id", "drifted_weight"])

    turnover = (
        weights.join(
            drifted,
            left_on=["observation_timestamp", "sec_id"],
            right_on=["next_timestamp", "sec_id"],
            how="left",
        )
        .with_columns(pl.col("drifted_weight").fill_null(0.0))
        .group_by("observation_timestamp")
        .agg((pl.col("weight") - pl.col("drifted_weight")).abs().sum().alias("turnover"))
    )

    cost_rate = simulation_config.transaction_cost_bps / 10_000.0
    return (
        gross_returns.join(turnover, on="observation_timestamp", how="left")
        .with_columns((pl.col("turnover") * cost_rate).alias("cost"))
        .with_columns((pl.col("gross_return") - pl.col("cost")).alias("net_return"))
        .sort("observation_timestamp")
        .with_columns(
            ((1.0 + pl.col("net_return")).cum_prod() * simulation_config.initial_capital).alias(
                "equity"
            )
        )
    )


def _require_columns(df: pl.DataFrame, required: set[str], label: str) -> None:
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"{label} missing required columns: {missing}")


def _empty_simulation() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "observation_timestamp": pl.Datetime,
            "gross_return": pl.Float64,
            "turnover": pl.Float64,
            "cost": pl.Float64,
            "net_return": pl.Float64,
            "equity": pl.Float64,
        }
    )


__all__ = ["SimulationConfig", "simulate_portfolio"]
