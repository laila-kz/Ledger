"""Bitemporal interval mathematics, immutable models, and derived bound utilities.

This module provides:
1. Mathematical interval structures for Valid Time [valid_from, valid_to) and
   Transaction / Knowledge Time [known_from, known_to).
2. SQL and Polars window-based derivation helpers for append-only storage.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ValidInterval(BaseModel):
    """Represents a Valid Time interval [valid_from, valid_to).

    Valid Time models when an event or state was true in the real-world domain
    (e.g., fiscal quarter ended 2023-06-30, or ticker active starting 2022-06-09).

    Invariants:
    - valid_from <= valid_to (when valid_to is not None).
    - An open interval (valid_to is None) represents a state that is currently still valid.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    valid_from: datetime | date
    valid_to: datetime | date | None = Field(
        default=None,
        description="Exclusive upper bound. None represents open validity [valid_from, +inf).",
    )

    @model_validator(mode="after")
    def validate_bounds(self) -> ValidInterval:
        """Ensure valid_from strictly precedes valid_to when valid_to is set."""
        if self.valid_to is not None:
            # Convert date to comparable datetime if types differ
            vf = self.valid_from
            vt = self.valid_to
            if isinstance(vf, datetime) and isinstance(vt, date) and not isinstance(vt, datetime):
                vt = datetime.combine(vt, datetime.min.time(), tzinfo=vf.tzinfo)
            elif isinstance(vt, datetime) and isinstance(vf, date) and not isinstance(vf, datetime):
                vf = datetime.combine(vf, datetime.min.time(), tzinfo=vt.tzinfo)

            if vf >= vt:
                raise ValueError(
                    f"Invalid interval bounds: valid_from ({self.valid_from}) "
                    f"must be strictly before valid_to ({self.valid_to})."
                )
        return self

    def contains(self, point: datetime | date) -> bool:
        """Check if a time point t falls within the half-open interval [valid_from, valid_to)."""
        if point < self.valid_from:
            return False
        return not (self.valid_to is not None and point >= self.valid_to)

    def overlaps(self, other: ValidInterval) -> bool:
        """Check if two valid intervals overlap."""
        self_end = self.valid_to
        other_end = other.valid_to

        if other_end is not None and self.valid_from >= other_end:
            return False
        return not (self_end is not None and other.valid_from >= self_end)


class TransactionInterval(BaseModel):
    """Represents a Transaction / Knowledge Time interval [known_from, known_to).

    Transaction Time models when a fact was known and actionable to the system
    (e.g., 10-Q filing accepted at 17:30 EST, or price bar published at 16:15 EST).

    Invariants:
    - known_from <= known_to (when known_to is not None).
    - An open interval (known_to is None) represents the currently active version of knowledge.
    - When a restatement occurs, known_to of the previous version is derived as the
      known_from of the superseding record.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    known_from: datetime
    known_to: datetime | None = Field(
        default=None,
        description="Exclusive upper bound. None represents active knowledge [known_from, +inf).",
    )

    @model_validator(mode="after")
    def validate_bounds(self) -> TransactionInterval:
        """Ensure known_from strictly precedes known_to when known_to is set."""
        if self.known_to is not None and self.known_from >= self.known_to:
            raise ValueError(
                f"Invalid transaction interval bounds: known_from ({self.known_from}) "
                f"must be strictly before known_to ({self.known_to})."
            )
        return self

    def is_known_at(self, observation_timestamp: datetime) -> bool:
        """Check if this fact was active knowledge at the given observation_timestamp.

        Returns True if: known_from <= observation_timestamp < known_to (or known_to is None).
        """
        if observation_timestamp < self.known_from:
            return False
        return not (self.known_to is not None and observation_timestamp >= self.known_to)


# =============================================================================
# Vectorized Derivation Helpers (Polars & DuckDB SQL)
# =============================================================================


def derive_known_to_polars(
    df: pl.DataFrame,
    partition_by: Sequence[str] | str,
    known_from_col: str = "known_from",
    seq_col: str = "ingestion_seq",
    known_to_col: str = "known_to",
) -> pl.DataFrame:
    """Derive `known_to` upper bounds across partitioned records using windowed LEAD().

    Sorts by `(known_from, ingestion_seq)` within each partition and computes:
        known_to = LEAD(known_from) OVER (PARTITION BY partition_by ORDER BY known_from, seq)

    Args:
        df: Input Polars DataFrame containing raw append-only rows.
        partition_by: Column(s) defining the entity partition (e.g., ['sec_id', 'metric_name']).
        known_from_col: Name of the transaction timestamp column.
        seq_col: Name of the monotonic sequence column used for deterministic tiebreaking.
        known_to_col: Output column name for derived upper bound.

    Returns:
        DataFrame with the new derived `known_to` column added.
    """
    p_cols = [partition_by] if isinstance(partition_by, str) else list(partition_by)

    return df.sort([*p_cols, known_from_col, seq_col]).with_columns(
        pl.col(known_from_col).shift(-1).over(p_cols).alias(known_to_col)
    )


def derive_valid_to_polars(
    df: pl.DataFrame,
    partition_by: Sequence[str] | str,
    valid_from_col: str = "valid_from",
    seq_col: str = "ingestion_seq",
    valid_to_col: str = "valid_to",
) -> pl.DataFrame:
    """Derive `valid_to` upper bounds across partitioned records using windowed LEAD().

    Sorts by `(valid_from, ingestion_seq)` within each partition and computes:
        valid_to = LEAD(valid_from) OVER (PARTITION BY partition_by ORDER BY valid_from, seq)

    Args:
        df: Input Polars DataFrame containing raw append-only rows.
        partition_by: Column(s) defining the entity partition (e.g., 'sec_id').
        valid_from_col: Name of the valid_from timestamp/date column.
        seq_col: Name of the monotonic sequence column used for tiebreaking.
        valid_to_col: Output column name for derived upper bound.

    Returns:
        DataFrame with the new derived `valid_to` column added.
    """
    p_cols = [partition_by] if isinstance(partition_by, str) else list(partition_by)

    return df.sort([*p_cols, valid_from_col, seq_col]).with_columns(
        pl.col(valid_from_col).shift(-1).over(p_cols).alias(valid_to_col)
    )


def build_bitemporal_view_sql(
    source_table: str,
    partition_by: Sequence[str] | str,
    known_from_col: str = "known_from",
    seq_col: str = "ingestion_seq",
    known_to_col: str = "known_to",
    columns: Sequence[str] | None = None,
) -> str:
    """Generate SQL SELECT query with dynamic LEAD() window derivation for DuckDB views.

    Example output:
    ```sql
    SELECT sec_id, metric_name, value, known_from, ingestion_seq,
           LEAD(known_from) OVER (
               PARTITION BY sec_id, metric_name
               ORDER BY known_from, ingestion_seq
           ) AS known_to
    FROM fact_fundamentals_raw
    ```
    """
    p_cols = [partition_by] if isinstance(partition_by, str) else list(partition_by)
    partition_str = ", ".join(p_cols)
    cols_str = ", ".join(columns) if columns else "*"

    return f"""
    SELECT
        {cols_str},
        LEAD({known_from_col}) OVER (
            PARTITION BY {partition_str}
            ORDER BY {known_from_col}, {seq_col}
        ) AS {known_to_col}
    FROM {source_table}
    """.strip()


def filter_point_in_time_polars(
    df: pl.DataFrame,
    as_of: datetime,
    known_from_col: str = "known_from",
    known_to_col: str = "known_to",
) -> pl.DataFrame:
    """Filter records to strictly those known and active as-of a given observation timestamp.

    Conditions:
        known_from <= as_of AND (known_to IS NULL OR as_of < known_to)
    """
    return df.filter(
        (pl.col(known_from_col) <= as_of)
        & (pl.col(known_to_col).is_null() | (as_of < pl.col(known_to_col)))
    )
