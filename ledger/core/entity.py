"""Entity resolution, permanent security identifiers (SecID), and bitemporal ticker mapping.

In financial markets, ticker symbols are mutable labels subject to renames (e.g. FB -> META),
mergers, delistings, and reuse. This module provides:
1. SecurityTickerMapping Pydantic model for append-only raw entity records.
2. DuckDB and Polars bitemporal view helpers deriving `valid_to` bounds via LEAD().
3. Point-in-time entity resolution: `resolve_sec_id` and inverse `resolve_ticker`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from ledger.core.bitemporal import derive_valid_to_polars


class SecurityTickerMapping(BaseModel):
    """Raw append-only mapping record linking a permanent SecID to a time-varying ticker symbol.

    Invariants:
    - `sec_id` is permanent and immutable (never changes for the economic entity).
    - `ticker` is valid starting from `valid_from`.
    - No `valid_to` column is physically stored; it is derived at query time via LEAD().
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sec_id: str = Field(description="Permanent unique identifier for the underlying security.")
    ticker: str = Field(description="Exchange ticker symbol active starting from valid_from.")
    valid_from: datetime | date = Field(
        description="Date or timestamp when this ticker became active.",
    )
    ingestion_seq: int = Field(
        default=1,
        ge=1,
        description="Monotonic sequence number for deterministic tiebreaking.",
    )


def create_ticker_map_view_sql(
    table_name: str = "dim_security_ticker_raw",
    view_name: str = "v_bitemporal_ticker_map",
) -> str:
    """Generate SQL creating the bitemporal ticker mapping view with derived `valid_to`."""
    return f"""
    CREATE OR REPLACE VIEW {view_name} AS
    SELECT
        sec_id,
        ticker,
        valid_from,
        ingestion_seq,
        LEAD(valid_from) OVER (
            PARTITION BY sec_id
            ORDER BY valid_from, ingestion_seq
        ) AS valid_to
    FROM {table_name};
    """.strip()


def register_ticker_map_view(
    conn: Any,
    table_name: str = "dim_security_ticker_raw",
    view_name: str = "v_bitemporal_ticker_map",
) -> None:
    """Register `v_bitemporal_ticker_map` view in a DuckDB connection."""
    sql = create_ticker_map_view_sql(table_name=table_name, view_name=view_name)
    conn.execute(sql)


def resolve_sec_id(
    ticker: str,
    as_of: date | datetime | str,
    conn: Any | None = None,
    df: pl.DataFrame | None = None,
    view_name: str = "v_bitemporal_ticker_map",
) -> str | None:
    """Resolve a ticker symbol to its permanent `sec_id` as of a specific point in time.

    Query Contract:
        Matches records where:
        ticker = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)

    Args:
        ticker: The ticker symbol to resolve (e.g. 'FB', 'META').
        as_of: The point-in-time date or timestamp.
        conn: Optional DuckDB connection containing the bitemporal ticker view.
        df: Optional Polars DataFrame containing raw or derived ticker mappings.
        view_name: Name of the DuckDB view to query when using `conn`.

    Returns:
        The permanent `sec_id` string if found, otherwise None.
    """
    ticker_clean = ticker.strip().upper()

    if conn is not None:
        as_of_val = as_of.isoformat() if isinstance(as_of, (date, datetime)) else str(as_of)
        query = f"""
        SELECT sec_id
        FROM {view_name}
        WHERE UPPER(ticker) = ?
          AND valid_from <= ?
          AND (valid_to IS NULL OR valid_to > ?)
        ORDER BY valid_from DESC, ingestion_seq DESC
        LIMIT 1;
        """
        result = conn.execute(query, [ticker_clean, as_of_val, as_of_val]).fetchone()
        return str(result[0]) if result else None

    if df is not None:
        # If valid_to is not yet derived, derive it
        if "valid_to" in df.columns:
            derived_df = df
        else:
            derived_df = derive_valid_to_polars(df=df, partition_by="sec_id")

        # Normalize as_of to datetime or date matching column dtype
        as_of_dt: datetime | date
        if isinstance(as_of, str):
            try:
                as_of_dt = datetime.fromisoformat(as_of)
            except ValueError:
                as_of_dt = date.fromisoformat(as_of)
        else:
            as_of_dt = as_of

        filtered = derived_df.filter(
            (pl.col("ticker").str.to_uppercase() == ticker_clean)
            & (pl.col("valid_from") <= as_of_dt)
            & (pl.col("valid_to").is_null() | (pl.col("valid_to") > as_of_dt))
        ).sort(["valid_from", "ingestion_seq"], descending=True)

        if len(filtered) > 0:
            return str(filtered["sec_id"][0])
        return None

    raise ValueError("Either 'conn' (DuckDB) or 'df' (Polars) must be provided to resolve_sec_id.")


def resolve_ticker(
    sec_id: str,
    as_of: date | datetime | str,
    conn: Any | None = None,
    df: pl.DataFrame | None = None,
    view_name: str = "v_bitemporal_ticker_map",
) -> str | None:
    """Inverse resolution: find the active ticker symbol for a `sec_id` as of a given date.

    Args:
        sec_id: The permanent security identifier (e.g. 'SEC_META').
        as_of: The point-in-time date or timestamp.
        conn: Optional DuckDB connection containing the bitemporal ticker view.
        df: Optional Polars DataFrame containing raw or derived ticker mappings.
        view_name: Name of the DuckDB view to query when using `conn`.

    Returns:
        The active ticker string if found, otherwise None.
    """
    sec_id_clean = sec_id.strip()

    if conn is not None:
        as_of_val = as_of.isoformat() if isinstance(as_of, (date, datetime)) else str(as_of)
        query = f"""
        SELECT ticker
        FROM {view_name}
        WHERE sec_id = ?
          AND valid_from <= ?
          AND (valid_to IS NULL OR valid_to > ?)
        ORDER BY valid_from DESC, ingestion_seq DESC
        LIMIT 1;
        """
        result = conn.execute(query, [sec_id_clean, as_of_val, as_of_val]).fetchone()
        return str(result[0]) if result else None

    if df is not None:
        if "valid_to" in df.columns:
            derived_df = df
        else:
            derived_df = derive_valid_to_polars(df=df, partition_by="sec_id")

        as_of_dt: datetime | date
        if isinstance(as_of, str):
            try:
                as_of_dt = datetime.fromisoformat(as_of)
            except ValueError:
                as_of_dt = date.fromisoformat(as_of)
        else:
            as_of_dt = as_of

        filtered = derived_df.filter(
            (pl.col("sec_id") == sec_id_clean)
            & (pl.col("valid_from") <= as_of_dt)
            & (pl.col("valid_to").is_null() | (pl.col("valid_to") > as_of_dt))
        ).sort(["valid_from", "ingestion_seq"], descending=True)

        if len(filtered) > 0:
            return str(filtered["ticker"][0])
        return None

    raise ValueError("Either 'conn' (DuckDB) or 'df' (Polars) must be provided to resolve_ticker.")
