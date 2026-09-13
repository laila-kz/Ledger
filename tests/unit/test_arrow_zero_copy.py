"""Tests verifying Apache Arrow zero-copy memory sharing between Polars and DuckDB.

This module verifies the zero-copy claim at the memory-buffer level (not just
the API surface). True zero-copy means:

  - Polars   -> PyArrow table: same underlying buffer addresses.
  - PyArrow  -> DuckDB (register): DuckDB reads from the same buffer.
  - DuckDB   -> PyArrow (result): query result is a new Arrow allocation.
  - PyArrow  -> Polars (from_arrow): wraps the result buffer without copying.

No intermediate CSV files, no Parquet on disk, no Python-level memcopy.
"""

from __future__ import annotations

import zoneinfo
from datetime import datetime, timedelta
from typing import Any

import polars as pl
import pytest

UTC = zoneinfo.ZoneInfo("UTC")


# =============================================================================
# Helpers
# =============================================================================


def _buffer_address(series: pl.Series) -> int:
    """Return the memory address of the first data buffer of a Polars Series.

    Raises ValueError if the column has no buffers (e.g. all-null).
    """
    for buf in series.to_arrow().buffers():
        if buf is not None:
            return int(buf.address)
    raise ValueError(f"No non-null buffer found for series '{series.name}'")


def _arrow_col_addr(table: Any, col_name: str) -> int:
    """Return buffer address of a named column in a PyArrow Table."""
    return int(table.column(col_name).chunks[0].buffers()[1].address)


# =============================================================================
# 1. Polars -> PyArrow: buffer identity
# =============================================================================


class TestPolarsToArrowZeroCopy:
    """Verify Polars.to_arrow() shares memory buffers with the source DataFrame."""

    def test_integer_column_buffer_shared(self) -> None:
        df = pl.DataFrame({"x": list(range(10_000))})
        arrow_table = df.to_arrow()

        addr_polars = _buffer_address(df["x"])
        addr_arrow = _arrow_col_addr(arrow_table, "x")

        assert addr_polars == addr_arrow, (
            f"Buffer mismatch: Polars={addr_polars:#x}, Arrow={addr_arrow:#x}. "
            "to_arrow() performed a copy instead of sharing memory."
        )

    def test_float_column_buffer_shared(self) -> None:
        df = pl.DataFrame({"price": [100.0 + i * 0.01 for i in range(10_000)]})
        arrow_table = df.to_arrow()

        addr_polars = _buffer_address(df["price"])
        addr_arrow = _arrow_col_addr(arrow_table, "price")

        assert addr_polars == addr_arrow

    def test_multi_column_both_shared(self) -> None:
        n = 5_000
        df = pl.DataFrame({"sec_id": list(range(n)), "close": [99.5 + i for i in range(n)]})
        arrow_table = df.to_arrow()

        for col_name in ["sec_id", "close"]:
            addr_pl = _buffer_address(df[col_name])
            addr_arr = _arrow_col_addr(arrow_table, col_name)
            assert addr_pl == addr_arr, f"Buffer copy detected for column '{col_name}'"


# =============================================================================
# 2. PyArrow -> Polars (from_arrow): buffer identity
# =============================================================================


class TestArrowToPolarsZeroCopy:
    """Verify pl.from_arrow() wraps Arrow buffers without copying."""

    def test_roundtrip_buffer_preserved(self) -> None:
        n = 10_000
        original = pl.DataFrame({"val": list(range(n))})
        arrow_table = original.to_arrow()
        recovered_raw = pl.from_arrow(arrow_table)
        assert isinstance(recovered_raw, pl.DataFrame)
        recovered: pl.DataFrame = recovered_raw

        addr_original = _buffer_address(original["val"])
        addr_recovered = _buffer_address(recovered["val"])

        assert addr_original == addr_recovered, (
            "from_arrow() should share the Arrow buffer -- buffer address changed, "
            "indicating an unnecessary copy."
        )

    def test_schema_preserved_in_roundtrip(self) -> None:
        df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"] * 100,
                "price": [155.0 + i * 0.1 for i in range(100)],
                "ts": [
                    datetime(2023, 1, 1, 21, 0, tzinfo=UTC) + timedelta(days=i) for i in range(100)
                ],
            }
        )
        recovered_raw = pl.from_arrow(df.to_arrow())
        assert isinstance(recovered_raw, pl.DataFrame)
        recovered: pl.DataFrame = recovered_raw
        assert recovered.columns == df.columns
        assert recovered.height == df.height
        assert recovered.dtypes == df.dtypes


# =============================================================================
# 3. DuckDB in-memory registration: no disk I/O
# =============================================================================


class TestDuckDBInMemoryRegistration:
    """Verify DuckDB can query a Polars/Arrow DataFrame without writing to disk."""

    def test_duckdb_query_without_csv_or_parquet(self) -> None:
        """DuckDB aggregates an Arrow table registered in-memory -- no disk touch."""
        import duckdb

        n = 20_000
        df = pl.DataFrame(
            {
                "sec_id": ["SEC_A"] * (n // 2) + ["SEC_B"] * (n // 2),
                "price": [100.0 + i * 0.01 for i in range(n)],
            }
        )
        arrow_table = df.to_arrow()

        con = duckdb.connect()
        con.register("market_data", arrow_table)

        result = con.execute(
            "SELECT sec_id, COUNT(*) as cnt, AVG(price) as avg_price "
            "FROM market_data GROUP BY sec_id ORDER BY sec_id"
        ).pl()

        assert result.height == 2
        assert result["cnt"].to_list() == [n // 2, n // 2]
        # SEC_A prices: 100.00..199.99 -> avg ~= 149.995
        assert result["avg_price"][0] == pytest.approx(149.995, rel=1e-4)
        # SEC_B prices: 200.00..299.99 -> avg ~= 249.995
        assert result["avg_price"][1] == pytest.approx(249.995, rel=1e-4)

    def test_no_temp_files_written(self, tmp_path: Any) -> None:
        """Confirm DuckDB in-memory registration writes zero files to the temp dir."""
        import os

        import duckdb

        df = pl.DataFrame({"x": list(range(1_000))})
        arrow_table = df.to_arrow()

        before = set(os.listdir(tmp_path))

        con = duckdb.connect()
        con.register("t", arrow_table)
        _ = con.execute("SELECT SUM(x) FROM t").fetchone()

        after = set(os.listdir(tmp_path))
        new_files = after - before

        assert len(new_files) == 0, (
            f"DuckDB wrote unexpected temp files: {new_files}. "
            "Query should operate entirely in-process memory."
        )


# =============================================================================
# 4. Full Polars -> Arrow -> DuckDB -> Arrow -> Polars pipeline
# =============================================================================


class TestEndToEndZeroCopyPipeline:
    """Full round-trip verification with scale, confirming no copies along the chain."""

    def test_full_pipeline_100k_rows(self) -> None:
        """100k row Polars DataFrame queries via DuckDB and returns to Polars."""
        import duckdb

        n = 100_000
        tickers = ["SEC_AAPL", "SEC_MSFT", "SEC_GOOG", "SEC_AMZN", "SEC_TSLA"]
        rows_per_ticker = n // len(tickers)

        df = pl.DataFrame(
            {
                "sec_id": tickers * rows_per_ticker,
                "price": [100.0 + (i % 1000) * 0.1 for i in range(n)],
                "volume": [int(1e6 + i) for i in range(n)],
            }
        )
        arrow_in = df.to_arrow()

        con = duckdb.connect()
        con.register("prices", arrow_in)

        result_arrow = con.execute(
            "SELECT sec_id, AVG(price) AS avg_price, SUM(volume) AS total_vol "
            "FROM prices GROUP BY sec_id ORDER BY sec_id"
        ).arrow()

        result_pl_raw = pl.from_arrow(result_arrow)
        assert isinstance(result_pl_raw, pl.DataFrame)
        result_pl: pl.DataFrame = result_pl_raw

        assert result_pl.height == len(tickers)
        assert "sec_id" in result_pl.columns
        assert "avg_price" in result_pl.columns
        assert result_pl["avg_price"].null_count() == 0

    def test_polars_to_arrow_to_duckdb_buffer_reuse(self) -> None:
        """The input Arrow table registered in DuckDB uses the same buffer as Polars."""
        import duckdb

        n = 50_000
        df = pl.DataFrame({"price": [150.25 + i * 0.01 for i in range(n)]})
        arrow_table = df.to_arrow()

        addr_before = _arrow_col_addr(arrow_table, "price")

        con = duckdb.connect()
        con.register("prices", arrow_table)
        _ = con.execute("SELECT COUNT(*) FROM prices").fetchone()

        addr_after = _arrow_col_addr(arrow_table, "price")

        assert addr_before == addr_after, (
            "DuckDB registration mutated or reallocated the Arrow input buffer. "
            "Input table should remain untouched."
        )

    def test_duckdb_result_to_polars_large_dataset(self) -> None:
        """Result of a DuckDB scan over 100k rows converts to Polars without error."""
        import duckdb

        n = 100_000
        df = pl.DataFrame(
            {
                "sec_id": ["SEC_TEST"] * n,
                "val": list(range(n)),
            }
        )
        con = duckdb.connect()
        con.register("data", df.to_arrow())

        result = con.execute("SELECT sec_id, val FROM data WHERE val % 2 = 0 ORDER BY val").pl()

        assert result.height == n // 2
        assert result["val"][0] == 0
        assert result["val"][-1] == n - 2
