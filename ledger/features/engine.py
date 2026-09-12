"""Vectorized bitemporal ASOF join engine for point-in-time feature extraction.

This module implements the core Point-in-Time (PIT) feature retrieval engine:
1. `ObservationMatrix`: Schema model validating `(sec_id, observation_timestamp)` inputs.
2. `join_features_as_of()`: Vectorized ASOF join engine executing point-in-time
   temporal joins against single or multiple feature views with derived `known_to`
   and `valid_to` interval boundary enforcement.

Mathematical & Architectural Invariants:
1. Valid Time / Market Time: Prices and fundamentals are matched strictly where
   `known_from <= observation_timestamp`.
2. Knowledge Upper Bounds: If a fact has a derived upper bound `known_to` or `valid_to`,
   it is invalid (masked to None) if `observation_timestamp >= known_to`.
3. Zero Look-Ahead: Facts arriving after `observation_timestamp` (filing lags,
   subsequent restatements, future corporate actions) are strictly invisible.
4. Vectorized Execution: Built on Polars `join_asof` and DuckDB Arrow zero-copy
   transport for sub-100ms execution across large observation universes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict, Field


class ObservationMatrix(BaseModel):
    """Schema model defining the input observation matrix.

    An observation matrix defines the evaluation coordinates (who and when)
    for portfolio decision-making and quantitative feature extraction.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    sec_id: str = Field(description="Permanent unique identifier for the security.")
    observation_timestamp: datetime = Field(
        description="Point-in-time timestamp at which features are evaluated."
    )


def validate_observation_matrix(
    entity_df: pl.DataFrame,
    sec_id_col: str = "sec_id",
    as_of_col: str = "observation_timestamp",
) -> pl.DataFrame:
    """Validate and normalise the observation matrix schema.

    Ensures:
    1. Mandatory columns `sec_id` and `observation_timestamp` exist.
    2. `sec_id` is Utf8/String type.
    3. `observation_timestamp` is Datetime type with UTC timezone.
    4. No null values exist in the primary identification columns.

    Args:
        entity_df: Input Polars DataFrame.
        sec_id_col: Name of the security identifier column.
        as_of_col: Name of the observation timestamp column.

    Returns:
        Validated and normalized Polars DataFrame.

    Raises:
        ValueError: If required columns are missing, null, or invalid.
    """
    if not isinstance(entity_df, pl.DataFrame):
        raise TypeError(f"entity_df must be a polars.DataFrame, got {type(entity_df).__name__}")

    missing = [c for c in [sec_id_col, as_of_col] if c not in entity_df.columns]
    if missing:
        raise ValueError(
            f"Observation matrix missing mandatory column(s): {missing}. "
            f"Available columns: {entity_df.columns}"
        )

    if entity_df.is_empty():
        return entity_df

    # Check for nulls in primary coordinates
    if entity_df[sec_id_col].null_count() > 0:
        raise ValueError(f"Observation matrix column '{sec_id_col}' contains null values.")
    if entity_df[as_of_col].null_count() > 0:
        raise ValueError(f"Observation matrix column '{as_of_col}' contains null values.")

    # Cast sec_id to String
    df = entity_df.with_columns(pl.col(sec_id_col).cast(pl.String))

    # Normalize observation timestamp to Datetime with UTC timezone
    dtype = df[as_of_col].dtype
    if dtype == pl.Date:
        df = df.with_columns(pl.col(as_of_col).cast(pl.Datetime("us")).dt.replace_time_zone("UTC"))
    elif isinstance(dtype, pl.Datetime):
        if dtype.time_zone is None:
            df = df.with_columns(pl.col(as_of_col).dt.replace_time_zone("UTC"))
        elif dtype.time_zone != "UTC":
            df = df.with_columns(pl.col(as_of_col).dt.convert_time_zone("UTC"))
    else:
        raise ValueError(f"Column '{as_of_col}' must be Date or Datetime, got {dtype}.")

    return df


def _normalize_temporal_column(
    df: pl.DataFrame,
    col_name: str,
) -> pl.DataFrame:
    """Normalize a temporal column to UTC Datetime if present."""
    if col_name not in df.columns:
        return df

    dtype = df[col_name].dtype
    if dtype == pl.Date:
        return df.with_columns(pl.col(col_name).cast(pl.Datetime("us")).dt.replace_time_zone("UTC"))
    if isinstance(dtype, pl.Datetime):
        if dtype.time_zone is None:
            return df.with_columns(pl.col(col_name).dt.replace_time_zone("UTC"))
        if dtype.time_zone != "UTC":
            return df.with_columns(pl.col(col_name).dt.convert_time_zone("UTC"))
    return df


def _detect_right_temporal_key(
    feature_df: pl.DataFrame,
    preferred_key: str | None = None,
) -> str:
    """Detect the transaction/temporal join key in the feature DataFrame.

    Rules:
    1. If `preferred_key` is provided, use it (validating column existence).
    2. If both `known_from` and `valid_from` are present, raise ValueError requiring
       the caller to explicitly specify `right_on` (bitemporal disambiguation).
    3. If `known_from` is present, use it (canonical transaction time).
    4. If `valid_from` is present, use it (canonical entity/validity start).
    5. Otherwise, check single fallback candidate (acceptance_time, timestamp).
    """
    if preferred_key:
        if preferred_key not in feature_df.columns:
            raise ValueError(
                f"Specified `right_on='{preferred_key}'` not found in "
                f"feature DataFrame columns: {feature_df.columns}"
            )
        return preferred_key

    has_known_from = "known_from" in feature_df.columns
    has_valid_from = "valid_from" in feature_df.columns

    if has_known_from and has_valid_from:
        raise ValueError(
            "Ambiguous temporal keys found: both 'known_from' (transaction time) and "
            "'valid_from' (valid time) are present in the feature DataFrame. "
            "Please explicitly specify `right_on` (e.g., right_on='known_from')."
        )

    if has_known_from:
        return "known_from"

    if has_valid_from:
        return "valid_from"

    fallback_candidates = [
        "acceptance_time",
        "observation_timestamp",
        "timestamp",
    ]
    matches = [k for k in fallback_candidates if k in feature_df.columns]

    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous fallback temporal keys found: {matches}. "
            f"Please explicitly specify `right_on`."
        )

    raise ValueError(
        f"Could not automatically detect temporal join key in feature DataFrame. "
        f"Available columns: {feature_df.columns}. Please explicitly pass `right_on`."
    )


def _detect_bound_key(
    feature_df: pl.DataFrame,
    preferred_bound: str | None = None,
) -> str | None:
    """Detect derived upper bound column (known_to or valid_to) for interval filtering.

    If multiple candidate bounds exist without explicit specification, raises ValueError.
    """
    if preferred_bound:
        if preferred_bound not in feature_df.columns:
            raise ValueError(
                f"Specified `bound_column='{preferred_bound}'` not found in "
                f"feature DataFrame: {feature_df.columns}"
            )
        return preferred_bound

    candidate_bounds = [
        "derived_known_to",
        "known_to",
        "derived_valid_to",
        "valid_to",
    ]
    matches = [b for b in candidate_bounds if b in feature_df.columns]

    if not matches:
        return None

    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous upper bound columns found in feature DataFrame: {matches}. "
            f"Please explicitly specify `bound_column` (e.g. bound_column='known_to')."
        )

    return matches[0]


def join_single_feature_as_of(
    entity_df: pl.DataFrame,
    feature_df: pl.DataFrame,
    as_of_column: str = "observation_timestamp",
    sec_id_column: str = "sec_id",
    right_on: str | None = None,
    right_sec_id_column: str | None = None,
    bound_column: str | None = None,
    keep_temporal_metadata: bool = False,
    feature_prefix: str | None = None,
) -> pl.DataFrame:
    """Join an observation matrix against a single feature DataFrame point-in-time.

    Executes a Polars backward ASOF join on `(sec_id, observation_timestamp)` and
    enforces upper bound constraints (`known_to` / `valid_to`).

    Args:
        entity_df: Normalized observation matrix DataFrame.
        feature_df: Feature view DataFrame containing temporal facts.
        as_of_column: Left join timestamp column in `entity_df`.
        sec_id_column: Left join security ID column in `entity_df`.
        right_on: Optional explicit temporal column in `feature_df` (e.g., 'known_from').
        right_sec_id_column: Optional explicit security ID column in `feature_df`.
        bound_column: Optional explicit upper bound column (e.g., 'known_to', 'valid_to').
        keep_temporal_metadata: If False, drops right temporal metadata columns.
        feature_prefix: Optional prefix to prepend to joined feature column names.

    Returns:
        Joined Polars DataFrame with point-in-time feature values.
    """
    if entity_df.is_empty():
        return entity_df

    r_sec_id = right_sec_id_column or sec_id_column
    if r_sec_id not in feature_df.columns:
        raise ValueError(
            f"Feature DataFrame missing security ID column '{r_sec_id}'. "
            f"Available columns: {feature_df.columns}"
        )

    r_time = _detect_right_temporal_key(feature_df, preferred_key=right_on)
    r_bound = _detect_bound_key(feature_df, preferred_bound=bound_column)

    # Normalize temporal columns in feature_df
    feat = _normalize_temporal_column(feature_df, r_time)
    if r_bound:
        feat = _normalize_temporal_column(feat, r_bound)

    # Ensure sec_id types match (String)
    feat = feat.with_columns(pl.col(r_sec_id).cast(pl.String))

    # Identify non-key feature columns to bring over
    reserved = {r_sec_id, r_time}
    if r_bound:
        reserved.add(r_bound)
    if "ingestion_seq" in feat.columns:
        reserved.add("ingestion_seq")

    feature_cols = [c for c in feat.columns if c not in reserved]

    # Handle column prefixes and collisions
    renamed_cols: dict[str, str] = {}
    for col in feature_cols:
        target_name = f"{feature_prefix}_{col}" if feature_prefix else col
        if target_name in entity_df.columns and target_name != sec_id_column:
            target_name = f"{target_name}_feature"
        renamed_cols[col] = target_name

    if renamed_cols:
        feat = feat.rename(renamed_cols)
        feature_cols = list(renamed_cols.values())

    # Sort left and right for ASOF join
    # Track original row order using a temporary index
    orig_col = "_ledger_row_order_idx"
    left_sorted = entity_df.with_columns(pl.int_range(0, pl.len()).alias(orig_col)).sort(
        as_of_column
    )
    right_sorted = feat.sort(r_time)

    # Execute backward ASOF join
    joined = left_sorted.join_asof(
        right_sorted,
        left_on=as_of_column,
        right_on=r_time,
        by_left=sec_id_column,
        by_right=r_sec_id,
        strategy="backward",
    )

    # Enforce upper bound constraint (known_to / valid_to)
    # If observation_timestamp >= bound, mask joined feature values to null
    if r_bound and r_bound in joined.columns:
        is_expired = pl.col(r_bound).is_not_null() & (pl.col(as_of_column) >= pl.col(r_bound))
        if feature_cols:
            joined = joined.with_columns(
                [
                    pl.when(is_expired).then(None).otherwise(pl.col(col)).alias(col)
                    for col in feature_cols
                ]
            )

    # Clean up temporal metadata if requested
    cols_to_drop: list[str] = []
    if not keep_temporal_metadata:
        for temp_col in [r_time, r_bound, "ingestion_seq"]:
            if temp_col and temp_col in joined.columns and temp_col != as_of_column:
                cols_to_drop.append(temp_col)

    if cols_to_drop:
        joined = joined.drop(cols_to_drop)

    # Restore original row ordering
    result = joined.sort(orig_col).drop(orig_col)
    return result


def join_features_as_of(
    entity_df: pl.DataFrame,
    feature_views: Sequence[str | pl.DataFrame | Any],
    catalog: Any | None = None,
    as_of_column: str = "observation_timestamp",
    sec_id_column: str = "sec_id",
    right_on: str | None = None,
    bound_column: str | None = None,
    keep_temporal_metadata: bool = False,
) -> pl.DataFrame:
    """Performs a bitemporal point-in-time ASOF join across one or more feature views.

    Invariants:
    1. Point-in-Time Availability: Matches records where `known_from <= observation_timestamp`.
    2. Dynamic Interval Invalidation: If a record has `known_to` or `valid_to` derived,
       it is invalidated (nullified) when `observation_timestamp >= known_to`.
    3. Zero Look-Ahead: Facts recorded after `observation_timestamp` are strictly excluded.
    4. Multi-View Composable: Successively joins multiple feature views against the
       observation matrix while preserving entity observation rows.

    Args:
        entity_df: Input observation matrix DataFrame containing `[sec_id, observation_timestamp]`.
        feature_views: List of feature view names (resolved via `catalog`) or Polars DataFrames.
        catalog: Optional `LedgerCatalog` or DuckDB connection used to resolve string view names.
        as_of_column: Name of the observation timestamp column.
        sec_id_column: Name of the security ID column.
        right_on: Optional explicit temporal join key in feature views (default: auto-detected).
        bound_column: Optional explicit upper bound column (default: auto-detected).
        keep_temporal_metadata: Whether to retain right-side `known_from`/`valid_from` columns.

    Returns:
        Polars DataFrame containing the observation matrix with joined feature columns.
    """
    normalized_entity = validate_observation_matrix(
        entity_df=entity_df,
        sec_id_col=sec_id_column,
        as_of_col=as_of_column,
    )

    if not feature_views:
        return normalized_entity

    result_df = normalized_entity

    for feature_source in feature_views:
        # Resolve feature source to a Polars DataFrame
        if isinstance(feature_source, pl.DataFrame):
            f_df = feature_source
        elif isinstance(feature_source, str):
            if catalog is None:
                raise ValueError(
                    f"Catalog or DuckDB connection required to resolve "
                    f"feature view name: '{feature_source}'. "
                    f"Pass `catalog=catalog` or provide a Polars DataFrame directly."
                )
            if hasattr(catalog, "query"):
                f_df = catalog.query(f"SELECT * FROM {feature_source}")
            elif hasattr(catalog, "execute"):
                f_df = catalog.execute(f"SELECT * FROM {feature_source}").pl()
            else:
                raise TypeError(
                    f"Unsupported catalog type: {type(catalog).__name__}. "
                    f"Expected LedgerCatalog or duckdb.DuckDBPyConnection."
                )
        elif hasattr(feature_source, "to_polars"):
            f_df = feature_source.to_polars()
        else:
            raise TypeError(
                f"Unsupported feature view type: {type(feature_source).__name__}. "
                f"Expected str or polars.DataFrame."
            )

        result_df = join_single_feature_as_of(
            entity_df=result_df,
            feature_df=f_df,
            as_of_column=as_of_column,
            sec_id_column=sec_id_column,
            right_on=right_on,
            bound_column=bound_column,
            keep_temporal_metadata=keep_temporal_metadata,
        )

    return result_df
