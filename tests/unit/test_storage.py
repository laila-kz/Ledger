"""Unit tests for storage layer, Parquet partitioning, ingestion logging, and DuckDB catalog."""

from datetime import date, datetime
from importlib.util import find_spec
from pathlib import Path

import polars as pl
import pytest

from ledger.storage.catalog import LedgerCatalog
from ledger.storage.ingestion_log import IngestionLogManager
from ledger.storage.partitions import (
    compute_file_sha256,
    write_atomic_parquet,
    write_entity_map,
    write_partitioned_corporate_actions,
    write_partitioned_market_ohlcv,
)


@pytest.fixture
def temp_storage_dir(tmp_path: Path) -> Path:
    """Create a temporary root storage directory."""
    raw_dir = tmp_path / "data" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    return raw_dir


class TestParquetPartitions:
    """Tests for atomic Parquet writing and Hive directory partitioning."""

    def test_write_atomic_parquet(self, temp_storage_dir: Path) -> None:
        target = temp_storage_dir / "test_file.parquet"
        df = pl.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})

        meta = write_atomic_parquet(df, target)

        assert target.exists()
        assert meta.row_count == 3
        assert meta.file_size_bytes > 0
        assert len(meta.sha256_hash) == 64
        # Verify hash matches recomputed hash
        assert meta.sha256_hash == compute_file_sha256(target)

    def test_market_ohlcv_monthly_partitioning(self, temp_storage_dir: Path) -> None:
        df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL", "SEC_AAPL", "SEC_AAPL"],
                "trade_date": [
                    date(2023, 1, 15),
                    date(2023, 1, 16),
                    date(2023, 2, 10),
                ],
                "open": [150.0, 151.0, 155.0],
                "high": [152.0, 153.0, 156.0],
                "low": [149.0, 150.5, 154.0],
                "close": [151.5, 152.0, 155.5],
                "volume": [1000000, 1200000, 1100000],
                "known_from": [
                    datetime(2023, 1, 15, 16, 15),
                    datetime(2023, 1, 16, 16, 15),
                    datetime(2023, 2, 10, 16, 15),
                ],
                "ingestion_seq": [1, 1, 1],
            }
        )

        written_files = write_partitioned_market_ohlcv(
            df=df,
            base_dir=temp_storage_dir,
            ingestion_seq=1,
        )

        assert len(written_files) == 2  # 2023-01 and 2023-02 partitions

        jan_file = temp_storage_dir / "market_ohlcv" / "year=2023" / "month=01"
        feb_file = temp_storage_dir / "market_ohlcv" / "year=2023" / "month=02"

        assert jan_file.exists()
        assert feb_file.exists()
        assert len(list(jan_file.glob("batch_000001_*.parquet"))) == 1
        assert len(list(feb_file.glob("batch_000001_*.parquet"))) == 1

    def test_corporate_actions_yearly_partitioning(self, temp_storage_dir: Path) -> None:
        df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL", "SEC_AAPL"],
                "action_type": ["SPLIT", "CASH_DIVIDEND"],
                "ex_date": [date(2020, 8, 31), date(2021, 5, 10)],
                "split_ratio": [4.0, None],
                "cash_amount": [None, 0.22],
                "announcement_date": [date(2020, 7, 30), date(2021, 4, 28)],
                "known_from": [
                    datetime(2020, 7, 30, 17, 0),
                    datetime(2021, 4, 28, 17, 0),
                ],
                "ingestion_seq": [1, 1],
            }
        )

        written_files = write_partitioned_corporate_actions(
            df=df,
            base_dir=temp_storage_dir,
            ingestion_seq=1,
        )

        assert len(written_files) == 2  # 2020 and 2021 partitions
        p_2020 = temp_storage_dir / "corporate_actions" / "year=2020"
        p_2021 = temp_storage_dir / "corporate_actions" / "year=2021"

        assert p_2020.exists()
        assert p_2021.exists()

    def test_entity_map_flat_partitioning(self, temp_storage_dir: Path) -> None:
        df = pl.DataFrame(
            {
                "sec_id": ["SEC_META", "SEC_META"],
                "ticker": ["FB", "META"],
                "valid_from": [datetime(2012, 5, 18), datetime(2022, 6, 9)],
                "ingestion_seq": [1, 2],
            }
        )

        written_files = write_entity_map(
            df=df,
            base_dir=temp_storage_dir,
            ingestion_seq=2,
        )

        assert len(written_files) == 1
        entity_dir = temp_storage_dir / "entity_map"
        assert len(list(entity_dir.glob("batch_000002_*.parquet"))) == 1


class TestIngestionLogManager:
    """Tests for monotonic ingestion sequence generation and audit logs."""

    def test_sequence_increment_and_persistence(self, temp_storage_dir: Path) -> None:
        mgr = IngestionLogManager(base_dir=temp_storage_dir)

        assert mgr.get_latest_seq() == 0
        assert mgr.get_next_seq() == 1

        # Write dummy file and record batch
        dummy_df = pl.DataFrame({"x": [1, 2]})
        dummy_file = temp_storage_dir / "entity_map" / "batch_000001_abc.parquet"
        meta1 = write_atomic_parquet(dummy_df, dummy_file)

        entry1 = mgr.record_batch(
            table_name="entity_map",
            source="manual_test",
            files_metadata=[meta1],
        )

        assert entry1.ingestion_seq == 1
        assert entry1.row_count == 2
        assert mgr.get_latest_seq() == 1
        assert mgr.get_next_seq() == 2

        # Record second batch
        file2 = temp_storage_dir / "entity_map" / "batch_000002_def.parquet"
        meta2 = write_atomic_parquet(dummy_df, file2)
        entry2 = mgr.record_batch(
            table_name="entity_map",
            source="manual_test",
            files_metadata=[meta2],
        )

        assert entry2.ingestion_seq == 2
        assert mgr.get_latest_seq() == 2

        # Verify history DataFrame
        history = mgr.get_history()
        assert len(history) == 2
        assert list(history["ingestion_seq"]) == [1, 2]


class TestLedgerCatalog:
    """Tests for DuckDB catalog and Hive partitioned query views."""

    def test_empty_catalog_initialization(self, temp_storage_dir: Path) -> None:
        if find_spec("duckdb") is None:
            pytest.skip("DuckDB not installed in environment yet.")

        catalog = LedgerCatalog(base_dir=temp_storage_dir)
        # Empty schema placeholders should exist and return 0 rows
        df_ohlcv = catalog.query("SELECT * FROM fact_market_ohlcv_raw")
        assert len(df_ohlcv) == 0
        assert "sec_id" in df_ohlcv.columns
        assert "close" in df_ohlcv.columns

        catalog.close()

    def test_catalog_query_with_populated_partitions(self, temp_storage_dir: Path) -> None:
        if find_spec("duckdb") is None:
            pytest.skip("DuckDB not installed in environment yet.")

        # Write sample market data
        df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL", "SEC_AAPL"],
                "trade_date": [date(2023, 1, 15), date(2023, 2, 15)],
                "open": [150.0, 160.0],
                "high": [155.0, 165.0],
                "low": [148.0, 158.0],
                "close": [153.0, 162.0],
                "volume": [5000000, 6000000],
                "known_from": [
                    datetime(2023, 1, 15, 16, 15),
                    datetime(2023, 2, 15, 16, 15),
                ],
                "ingestion_seq": [1, 1],
            }
        )
        write_partitioned_market_ohlcv(df=df, base_dir=temp_storage_dir, ingestion_seq=1)

        # Write entity mapping
        entity_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL"],
                "ticker": ["AAPL"],
                "valid_from": [datetime(1980, 12, 12)],
                "ingestion_seq": [1],
            }
        )
        write_entity_map(df=entity_df, base_dir=temp_storage_dir, ingestion_seq=1)

        catalog = LedgerCatalog(base_dir=temp_storage_dir)

        # Query market OHLCV directly via registered DuckDB view
        res = catalog.query(
            "SELECT sec_id, trade_date, close FROM fact_market_ohlcv_raw ORDER BY trade_date"
        )
        assert len(res) == 2
        assert res["close"][0] == 153.0
        assert res["close"][1] == 162.0

        # Query derived bitemporal ticker map
        entity_res = catalog.query(
            "SELECT sec_id, ticker, valid_to FROM v_bitemporal_ticker_map WHERE ticker = 'AAPL'"
        )
        assert len(entity_res) == 1
        assert entity_res["sec_id"][0] == "SEC_AAPL"
        assert entity_res["valid_to"][0] is None

        catalog.close()
