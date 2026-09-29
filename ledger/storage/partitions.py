"""Parquet partitioning utilities, directory layout standards, and atomic file writers.

Layout Standard:
- market_ohlcv:       data/raw/market_ohlcv/year=YYYY/month=MM/batch_{ingestion_seq}_{uuid}.parquet
- corporate_actions:  data/raw/corporate_actions/year=YYYY/batch_{ingestion_seq}_{uuid}.parquet
- entity_map:         data/raw/entity_map/batch_{ingestion_seq}_{uuid}.parquet
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import NamedTuple

import polars as pl


class WrittenFileMetadata(NamedTuple):
    """Metadata for an atomically written Parquet partition file."""

    file_path: Path
    row_count: int
    file_size_bytes: int
    sha256_hash: str


def compute_file_sha256(file_path: Path | str) -> str:
    """Compute SHA-256 hex digest for a file."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(64 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def get_table_root_dir(base_dir: Path | str, table_name: str) -> Path:
    """Get canonical root directory for a given logical table."""
    return Path(base_dir) / table_name


def write_atomic_parquet(df: pl.DataFrame, target_path: Path) -> WrittenFileMetadata:
    """Write a Polars DataFrame to target_path atomically using a temporary file.

    Guarantees that partial writes never corrupt the destination file.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.parent / f".tmp_{uuid.uuid4().hex}_{target_path.name}"

    try:
        df.write_parquet(temp_path, compression="snappy")
        # Atomic file rename on POSIX and Windows (Python 3.3+ os.replace)
        os.replace(temp_path, target_path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise

    file_size = target_path.stat().st_size
    file_hash = compute_file_sha256(target_path)

    return WrittenFileMetadata(
        file_path=target_path,
        row_count=len(df),
        file_size_bytes=file_size,
        sha256_hash=file_hash,
    )


def filter_already_persisted(
    df: pl.DataFrame,
    base_dir: Path | str,
    table_dir: str,
    key_columns: list[str],
) -> pl.DataFrame:
    """Drop rows whose business key is already present on disk.

    Storage is append-only, so re-running an ingestion would otherwise append a
    second identical copy of every row. That broke the ``IdempotentReplay``
    invariant the bitemporal model is verified against: seeding eight times
    produced 48,288 rows for 6,036 distinct ``(sec_id, trade_date)`` keys, and
    the duplication compounded wherever an aggregate met the data.

    A row is considered already persisted when its business key is present with
    identical payloads. A key present with *different* values is a genuine
    correction and is kept, because a bitemporal store must retain the
    superseding fact rather than silently drop it.

    Returns the rows that still need writing; the caller keeps its existing
    "no new rows" behaviour when this is empty.
    """
    root_dir = get_table_root_dir(base_dir, table_dir)
    if not root_dir.exists():
        return df

    existing_files = sorted(root_dir.glob("**/*.parquet"))
    if not existing_files:
        return df

    existing = pl.concat(
        [pl.read_parquet(path) for path in existing_files],
        how="diagonal_relaxed",
    )

    shared_keys = [c for c in key_columns if c in existing.columns]
    if len(shared_keys) != len(key_columns):
        return df

    # Compare on business columns only; ingestion_seq legitimately differs.
    payload_columns = [c for c in df.columns if c not in ("ingestion_seq",)]
    shared_payload = [c for c in payload_columns if c in existing.columns]
    if not shared_payload:
        return df

    known = existing.select(shared_payload).unique()
    # nulls_equal matters: corporate actions carry null currency/cash_amount, and
    # a default join treats NULL != NULL, so a pure replay would look like new
    # data and get appended forever.
    return df.join(known, on=shared_payload, how="anti", nulls_equal=True)


def write_partitioned_market_ohlcv(
    df: pl.DataFrame,
    base_dir: Path | str = "data/raw",
    ingestion_seq: int = 1,
    date_col: str = "trade_date",
) -> list[WrittenFileMetadata]:
    """Write market OHLCV data partitioned by year=YYYY/month=MM/ into append-only Parquet files.

    Each partition receives a new immutable file: batch_{ingestion_seq}_{uuid}.parquet

    Re-ingesting an unchanged range is a no-op: rows already present on disk
    under the same business key and payload are filtered out first, so
    re-running a seed does not duplicate history. See ``filter_already_persisted``.
    """
    df = filter_already_persisted(
        df=df,
        base_dir=base_dir,
        table_dir="market_ohlcv",
        key_columns=["sec_id", date_col, "known_from"],
    )

    if len(df) == 0:
        return []

    root_dir = get_table_root_dir(base_dir, "market_ohlcv")
    written_files: list[WrittenFileMetadata] = []

    # Ensure date column is Date dtype and derive partition components
    df_with_parts = df.with_columns(
        pl.col(date_col).cast(pl.Date).dt.year().alias("__year"),
        pl.col(date_col).cast(pl.Date).dt.month().alias("__month"),
    )

    partitions = df_with_parts.partition_by(["__year", "__month"], as_dict=True)

    for (year_val, month_val), partition_df in partitions.items():
        # Remove helper partition columns before writing
        clean_df = partition_df.drop(["__year", "__month"])

        month_str = f"{month_val:02d}"
        partition_dir = root_dir / f"year={year_val}" / f"month={month_str}"
        file_id = uuid.uuid4().hex[:8]
        filename = f"batch_{ingestion_seq:06d}_{file_id}.parquet"
        target_path = partition_dir / filename

        meta = write_atomic_parquet(clean_df, target_path)
        written_files.append(meta)

    return written_files


def write_partitioned_corporate_actions(
    df: pl.DataFrame,
    base_dir: Path | str = "data/raw",
    ingestion_seq: int = 1,
    date_col: str = "ex_date",
) -> list[WrittenFileMetadata]:
    """Write corporate actions data partitioned by year=YYYY/ into append-only Parquet files.

    Each partition receives a new immutable file: batch_{ingestion_seq}_{uuid}.parquet

    Re-ingesting an unchanged window is a no-op, matching the market OHLCV
    writer, so the split table stays free of duplicate events.
    """
    df = filter_already_persisted(
        df=df,
        base_dir=base_dir,
        table_dir="corporate_actions",
        key_columns=["sec_id", "action_type", date_col, "known_from"],
    )

    if len(df) == 0:
        return []

    root_dir = get_table_root_dir(base_dir, "corporate_actions")
    written_files: list[WrittenFileMetadata] = []

    df_with_parts = df.with_columns(
        pl.col(date_col).cast(pl.Date).dt.year().alias("__year"),
    )

    partitions = df_with_parts.partition_by("__year", as_dict=True)

    for (year_val,), partition_df in partitions.items():
        clean_df = partition_df.drop("__year")

        partition_dir = root_dir / f"year={year_val}"
        file_id = uuid.uuid4().hex[:8]
        filename = f"batch_{ingestion_seq:06d}_{file_id}.parquet"
        target_path = partition_dir / filename

        meta = write_atomic_parquet(clean_df, target_path)
        written_files.append(meta)

    return written_files


def write_entity_map(
    df: pl.DataFrame,
    base_dir: Path | str = "data/raw",
    ingestion_seq: int = 1,
) -> list[WrittenFileMetadata]:
    """Write entity mapping data (flat directory layout) into append-only Parquet files.

    Writes to: data/raw/entity_map/batch_{ingestion_seq}_{uuid}.parquet

    Re-registering an unchanged ticker set is a no-op.
    """
    df = filter_already_persisted(
        df=df,
        base_dir=base_dir,
        table_dir="entity_map",
        key_columns=["sec_id", "valid_from"],
    )

    if len(df) == 0:
        return []

    root_dir = get_table_root_dir(base_dir, "entity_map")
    file_id = uuid.uuid4().hex[:8]
    filename = f"batch_{ingestion_seq:06d}_{file_id}.parquet"
    target_path = root_dir / filename

    meta = write_atomic_parquet(df, target_path)
    return [meta]
