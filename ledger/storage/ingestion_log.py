"""Monotonic ingestion sequence counter, audit trail logger, and batch metadata tracker.

Every ingestion batch is assigned a unique, monotonically increasing `ingestion_seq`.
This guarantees:
1. Deterministic window ordering for derived `known_to` and `valid_to` bounds.
2. Complete auditability of every data batch, row count, timestamp, and SHA-256 file hashes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from ledger.storage.partitions import WrittenFileMetadata, write_atomic_parquet


class IngestionLogEntry(BaseModel):
    """Metadata record for a single ingestion batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ingestion_seq: int = Field(ge=1, description="Monotonic sequence number for this batch.")
    ingested_at: datetime = Field(description="UTC timestamp when the batch was committed.")
    table_name: str = Field(description="Target logical table name.")
    source: str = Field(description="Data provider / source identifier (e.g. yfinance, SEC).")
    row_count: int = Field(ge=0, description="Total rows written across all partitions.")
    file_count: int = Field(ge=0, description="Total Parquet partition files written.")
    files_written: list[str] = Field(description="Relative file paths of written partition files.")
    file_hashes: list[str] = Field(description="SHA-256 digests for each written file.")


class IngestionLogManager:
    """Manages persistent ingestion audit logs and sequence generation.

    Storage location: {base_dir}/metadata/ingestion_log.parquet
    """

    def __init__(self, base_dir: Path | str = "data/raw") -> None:
        self.base_dir = Path(base_dir)
        self.metadata_dir = self.base_dir / "metadata"
        self.log_file = self.metadata_dir / "ingestion_log.parquet"

    def _read_log_df(self) -> pl.DataFrame:
        """Read existing log file or return empty structured DataFrame."""
        if not self.log_file.exists():
            return pl.DataFrame(
                schema={
                    "ingestion_seq": pl.Int64,
                    "ingested_at": pl.Datetime("us", "UTC"),
                    "table_name": pl.Utf8,
                    "source": pl.Utf8,
                    "row_count": pl.Int64,
                    "file_count": pl.Int64,
                    "files_written": pl.List(pl.Utf8),
                    "file_hashes": pl.List(pl.Utf8),
                }
            )
        return pl.read_parquet(self.log_file)

    def get_latest_seq(self) -> int:
        """Get the highest ingestion_seq allocated so far (0 if no batches exist)."""
        df = self._read_log_df()
        if len(df) == 0:
            return 0
        return int(df["ingestion_seq"].max())  # type: ignore[arg-type]

    def get_next_seq(self) -> int:
        """Allocate the next sequential ingestion_seq."""
        return self.get_latest_seq() + 1

    def record_batch(
        self,
        table_name: str,
        source: str,
        files_metadata: Sequence[WrittenFileMetadata],
        ingestion_seq: int | None = None,
    ) -> IngestionLogEntry:
        """Record a newly written batch to the persistent ingestion log.

        Args:
            table_name: Name of target table (e.g. 'market_ohlcv').
            source: Source descriptor (e.g. 'yfinance').
            files_metadata: List of WrittenFileMetadata records from the partition writer.
            ingestion_seq: Optional pre-allocated sequence number; allocated if None.

        Returns:
            The committed IngestionLogEntry record.
        """
        if ingestion_seq is None:
            ingestion_seq = self.get_next_seq()

        total_rows = sum(m.row_count for m in files_metadata)
        files_written = [
            str(m.file_path.relative_to(self.base_dir)).replace("\\", "/") for m in files_metadata
        ]
        file_hashes = [m.sha256_hash for m in files_metadata]

        entry = IngestionLogEntry(
            ingestion_seq=ingestion_seq,
            ingested_at=datetime.now(timezone.utc),
            table_name=table_name,
            source=source,
            row_count=total_rows,
            file_count=len(files_metadata),
            files_written=files_written,
            file_hashes=file_hashes,
        )

        # Append to log DataFrame and save atomically
        existing_df = self._read_log_df()
        new_row = pl.DataFrame(
            {
                "ingestion_seq": [entry.ingestion_seq],
                "ingested_at": [entry.ingested_at],
                "table_name": [entry.table_name],
                "source": [entry.source],
                "row_count": [entry.row_count],
                "file_count": [entry.file_count],
                "files_written": [entry.files_written],
                "file_hashes": [entry.file_hashes],
            },
            schema={
                "ingestion_seq": pl.Int64,
                "ingested_at": pl.Datetime("us", "UTC"),
                "table_name": pl.Utf8,
                "source": pl.Utf8,
                "row_count": pl.Int64,
                "file_count": pl.Int64,
                "files_written": pl.List(pl.Utf8),
                "file_hashes": pl.List(pl.Utf8),
            },
        )

        updated_df = pl.concat([existing_df, new_row])
        write_atomic_parquet(updated_df, self.log_file)

        return entry

    def get_history(self, table_name: str | None = None) -> pl.DataFrame:
        """Get the full ingestion history or filtered by table."""
        df = self._read_log_df()
        if table_name is not None and len(df) > 0:
            df = df.filter(pl.col("table_name") == table_name)
        return df
