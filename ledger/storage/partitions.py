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


def write_partitioned_market_ohlcv(
    df: pl.DataFrame,
    base_dir: Path | str = "data/raw",
    ingestion_seq: int = 1,
    date_col: str = "trade_date",
) -> list[WrittenFileMetadata]:
    """Write market OHLCV data partitioned by year=YYYY/month=MM/ into append-only Parquet files.

    Each partition receives a new immutable file: batch_{ingestion_seq}_{uuid}.parquet
    """
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
    """
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
    """
    if len(df) == 0:
        return []

    root_dir = get_table_root_dir(base_dir, "entity_map")
    file_id = uuid.uuid4().hex[:8]
    filename = f"batch_{ingestion_seq:06d}_{file_id}.parquet"
    target_path = root_dir / filename

    meta = write_atomic_parquet(df, target_path)
    return [meta]
