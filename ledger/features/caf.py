"""Dynamic Cumulative Adjustment Factor (CAF) engine.

The CAF is the mathematical core of the Ledger system's no-leakage guarantee.
It answers the question: "If I am making a trading decision at time T_obs, what
adjustment factor should I apply to the raw close price of date t?"

Mathematical Definition
-----------------------
For a raw price date t and observation timestamp T_obs:

    CAF(t, T_obs) = PRODUCT of split_ratio_k
                    for all splits k where:
                        t < ex_date_k <= T_obs   (split happened after price date, on or before obs)
                        AND known_from_k <= T_obs (we actually knew about it by obs time)

Then:
    adj_close(t, T_obs) = raw_close(t) * CAF(t, T_obs)

Why This Prevents Lookahead Bias
---------------------------------
A static "adjusted" price series (e.g., from yfinance auto_adjust=True) applies ALL
future splits retroactively � so the July 2020 price of AAPL is already divided by 4
even when simulating a decision made in July 2020. This leaks the August 2020 split.

The CAF engine fixes this by computing the product dynamically:
- At T_obs = 2020-08-15: no splits are in scope -> CAF = 1.0 -> adj_close = raw_close.
- At T_obs = 2020-09-01: the 4:1 split (known_from ~= 2020-08-31 20:15 UTC) is in scope
  -> CAF = 4.0 -> adj_close = raw_close * 4.

The known_from guard is the final line of defence: even on the ex-date, if the split
hasn't been confirmed and recorded yet (e.g. before market close), CAF stays at 1.0.

Key Schemas
-----------
prices_df columns required:
    sec_id          : Utf8
    trade_date      : Date
    close           : Float64

splits_df columns required:
    sec_id          : Utf8
    ex_date         : Date
    split_ratio     : Float64
    known_from      : Datetime(time_unit="us", time_zone="UTC")

observation_df columns required:
    sec_id          : Utf8
    observation_timestamp  : Datetime(time_unit="us", time_zone="UTC")

Output columns (one row per (sec_id, observation_timestamp, trade_date) triple):
    sec_id, observation_timestamp, trade_date, close, caf, adj_close
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import polars as pl

# ---------------------------------------------------------------------------
# Public helpers for scalar / point-in-time CAF queries
# ---------------------------------------------------------------------------


def compute_caf_scalar(
    price_date: date,
    observation_timestamp: datetime,
    splits_df: pl.DataFrame,
    sec_id: str,
) -> float:
    """Compute the CAF for a single (sec_id, price_date, observation_timestamp) triple.

    This is the reference implementation used in unit tests. Production code
    should use the vectorized `compute_caf_matrix` for performance.

    Args:
        price_date: The historical date of the raw price (t).
        observation_timestamp: The decision timestamp (T_obs). Must be tz-aware UTC.
        splits_df: Corporate-actions DataFrame with columns
                   [sec_id, ex_date, split_ratio, known_from].
        sec_id: The security identifier to filter by.

    Returns:
        The CAF scalar (>= 1.0). Returns 1.0 if no qualifying splits exist.
    """
    if observation_timestamp.tzinfo is None:
        observation_timestamp = observation_timestamp.replace(tzinfo=timezone.utc)

    obs_date: date = observation_timestamp.date()

    splits_clean = _normalise_splits(splits_df)

    # Filter: splits for this security where t < ex_date <= T_obs AND known_from <= T_obs
    relevant = splits_clean.filter(
        (pl.col("sec_id") == sec_id)
        & (pl.col("ex_date") > price_date)  # happened AFTER price date
        & (pl.col("ex_date") <= obs_date)  # on or before observation date
        & (pl.col("known_from") <= pl.lit(observation_timestamp))  # known by obs time
    )

    if relevant.is_empty():
        return 1.0

    return float(relevant["split_ratio"].product())


# ---------------------------------------------------------------------------
# Vectorized CAF engine -- the production path
# ---------------------------------------------------------------------------


def compute_caf_matrix(
    prices_df: pl.DataFrame,
    splits_df: pl.DataFrame,
    observation_df: pl.DataFrame,
) -> pl.DataFrame:
    """Compute CAF and adjusted close for every (sec_id, trade_date, observation_timestamp) triple.

    This is the fully vectorized engine. It operates purely in Polars without
    Python-level loops, making it suitable for large observation matrices.

    Algorithm (vectorized, no row-by-row Python):
    1. Cross-join each (sec_id, observation_timestamp) x prices on sec_id.
    2. Left-join in splits on sec_id.
    3. Apply the three CAF filter predicates as Polars expressions in one pass.
    4. Group by (sec_id, observation_timestamp, trade_date) and product-aggregate qualifying ratios.
    5. Fill 1.0 for groups with no qualifying splits.
    6. Compute adj_close = close * caf.

    Args:
        prices_df: Raw OHLCV data with columns [sec_id, trade_date, close].
        splits_df: Corporate actions (SPLIT rows) with columns
                   [sec_id, ex_date, split_ratio, known_from].
        observation_df: Observation matrix with columns [sec_id, observation_timestamp].
                        Each row is one decision point in time.

    Returns:
        DataFrame with columns:
            sec_id, observation_timestamp, trade_date, close, caf, adj_close
        One row per (sec_id, observation_timestamp, trade_date) triple.

    Raises:
        ValueError: If required columns are missing from any input DataFrame.
    """
    _validate_prices_df(prices_df)
    _validate_splits_df(splits_df)
    _validate_observation_df(observation_df)

    splits_clean = _normalise_splits(splits_df)

    # ------------------------------------------------------------------
    # Step 1: Expand: (sec_id, obs_ts) x (trade_date, close) on sec_id
    # ------------------------------------------------------------------
    obs_x_prices = observation_df.join(
        prices_df.select(["sec_id", "trade_date", "close"]),
        on="sec_id",
        how="inner",
    )

    # ------------------------------------------------------------------
    # Step 2: Left-join splits on sec_id
    # ------------------------------------------------------------------
    obs_x_prices_x_splits = obs_x_prices.join(
        splits_clean.select(["sec_id", "ex_date", "split_ratio", "known_from"]),
        on="sec_id",
        how="left",
    )

    # ------------------------------------------------------------------
    # Step 3: Apply full CAF predicate as a conditional expression
    # trade_date < ex_date <= obs_date AND known_from <= observation_timestamp
    # ------------------------------------------------------------------
    obs_x_prices_x_splits = obs_x_prices_x_splits.with_columns(
        pl.col("observation_timestamp").dt.date().alias("_obs_date")
    ).with_columns(
        pl.when(
            pl.col("ex_date").is_not_null()
            & (pl.col("trade_date") < pl.col("ex_date"))
            & (pl.col("ex_date") <= pl.col("_obs_date"))
            & (pl.col("known_from") <= pl.col("observation_timestamp"))
        )
        .then(pl.col("split_ratio"))
        .otherwise(pl.lit(None, dtype=pl.Float64))
        .alias("_qualifying_ratio")
    )

    # ------------------------------------------------------------------
    # Step 4: Aggregate -- product of qualifying ratios per group
    # ------------------------------------------------------------------
    caf_df = (
        obs_x_prices_x_splits.group_by(["sec_id", "observation_timestamp", "trade_date", "close"])
        .agg(pl.col("_qualifying_ratio").drop_nulls().product().alias("caf"))
        .with_columns(
            # product() on empty series (all None) returns None -> fill with 1.0
            pl.col("caf").fill_null(1.0)
        )
    )

    # ------------------------------------------------------------------
    # Step 5: adj_close = close * caf
    # ------------------------------------------------------------------
    result = caf_df.with_columns((pl.col("close") * pl.col("caf")).alias("adj_close")).select(
        ["sec_id", "observation_timestamp", "trade_date", "close", "caf", "adj_close"]
    )

    return result


# ---------------------------------------------------------------------------
# Convenience wrapper: adjusted close series for a single observation time
# ---------------------------------------------------------------------------


def adjusted_close_as_of(
    prices_df: pl.DataFrame,
    splits_df: pl.DataFrame,
    observation_timestamp: datetime,
    sec_id: str,
) -> pl.DataFrame:
    """Return the adjusted close series for one security at a single observation time.

    Convenience wrapper around `compute_caf_matrix` for scalar observation points.

    Args:
        prices_df: Raw prices with [sec_id, trade_date, close].
        splits_df: Splits with [sec_id, ex_date, split_ratio, known_from].
        observation_timestamp: Single observation timestamp (must be tz-aware UTC).
        sec_id: The security to compute for.

    Returns:
        DataFrame with [trade_date, close, caf, adj_close] ordered by trade_date.
    """
    if observation_timestamp.tzinfo is None:
        observation_timestamp = observation_timestamp.replace(tzinfo=timezone.utc)

    obs_df = pl.DataFrame(
        {"sec_id": [sec_id], "observation_timestamp": [observation_timestamp]},
        schema={
            "sec_id": pl.Utf8,
            "observation_timestamp": pl.Datetime("us", "UTC"),
        },
    )

    result = compute_caf_matrix(
        prices_df=prices_df.filter(pl.col("sec_id") == sec_id),
        splits_df=splits_df.filter(pl.col("sec_id") == sec_id),
        observation_df=obs_df,
    )

    return result.drop(["sec_id", "observation_timestamp"]).sort("trade_date")


# ---------------------------------------------------------------------------
# Internal validation helpers
# ---------------------------------------------------------------------------

_PRICES_REQUIRED = {"sec_id", "trade_date", "close"}
_SPLITS_REQUIRED = {"sec_id", "ex_date", "split_ratio", "known_from"}
_OBS_REQUIRED = {"sec_id", "observation_timestamp"}


def _validate_prices_df(df: pl.DataFrame) -> None:
    missing = _PRICES_REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"prices_df is missing required columns: {missing}")


def _validate_splits_df(df: pl.DataFrame) -> None:
    missing = _SPLITS_REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"splits_df is missing required columns: {missing}")


def _validate_observation_df(df: pl.DataFrame) -> None:
    missing = _OBS_REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"observation_df is missing required columns: {missing}")


def _normalise_splits(splits_df: pl.DataFrame) -> pl.DataFrame:
    """Ensure known_from is a tz-aware UTC Datetime column."""
    dtype = splits_df["known_from"].dtype
    if dtype == pl.Date:
        return splits_df.with_columns(
            pl.col("known_from").cast(pl.Datetime("us")).dt.replace_time_zone("UTC")
        )
    if isinstance(dtype, pl.Datetime) and dtype.time_zone is None:
        return splits_df.with_columns(pl.col("known_from").dt.replace_time_zone("UTC"))
    return splits_df
