"""Unit tests for market data and corporate actions ingestion pipelines (offline with mocks)."""

from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import polars as pl
import pytest

from ledger.ingestion.corporate_actions import (
    ingest_corporate_actions,
    parse_dividends_series,
    parse_splits_series,
)
from ledger.ingestion.market_data import (
    TickerRegistry,
    ingest_ohlcv,
    parse_yfinance_ohlcv_dataframe,
)
from ledger.storage.catalog import LedgerCatalog


@pytest.fixture
def temp_raw_dir(tmp_path: Path) -> Path:
    """Temporary storage directory for ingestion testing."""
    raw_dir = tmp_path / "data" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    return raw_dir


class TestTickerRegistry:
    """Tests for synthetic SecID generator and entity map registration."""

    def test_get_or_create_sec_id(self) -> None:
        reg = TickerRegistry()
        assert reg.get_or_create_sec_id("aapl") == "SEC_AAPL_001"
        assert reg.get_or_create_sec_id("  MSFT  ") == "SEC_MSFT_001"

    def test_register_tickers_writes_parquet(self, temp_raw_dir: Path) -> None:
        reg = TickerRegistry(base_dir=temp_raw_dir)
        mappings = reg.register_tickers(["AAPL", "NVDA"], ingestion_seq=1)

        assert len(mappings) == 2
        assert mappings[0].sec_id == "SEC_AAPL_001"
        assert mappings[1].sec_id == "SEC_NVDA_001"

        entity_dir = temp_raw_dir / "entity_map"
        parquet_files = list(entity_dir.glob("*.parquet"))
        assert len(parquet_files) == 1

        df = pl.read_parquet(parquet_files[0])
        assert len(df) == 2
        assert set(df["ticker"]) == {"AAPL", "NVDA"}


class TestParseDataFrames:
    """Tests for parsing raw yfinance structures into typed schemas."""

    def test_parse_ohlcv_dataframe(self) -> None:
        raw_pdf = pd.DataFrame(
            {
                "Open": [100.0, 102.0],
                "High": [105.0, 106.0],
                "Low": [99.0, 101.0],
                "Close": [104.0, 105.0],
                "Adj Close": [25.0, 25.25],  # Should be completely ignored!
                "Volume": [1000000, 1500000],
            },
            index=pd.to_datetime(["2020-08-27", "2020-08-28"]),
        )

        parsed = parse_yfinance_ohlcv_dataframe(
            raw_df=raw_pdf,
            ticker="AAPL",
            sec_id="SEC_AAPL_001",
            ingestion_seq=1,
        )

        assert len(parsed) == 2
        # Verify strictly unadjusted Close (104.0 / 105.0), NOT Adj Close (25.0)
        assert parsed["close"][0] == 104.0
        assert parsed["close"][1] == 105.0
        assert "Adj Close" not in parsed.columns
        assert parsed["sec_id"][0] == "SEC_AAPL_001"
        assert isinstance(parsed["trade_date"][0], date)
        assert isinstance(parsed["known_from"][0], datetime)

    def test_parse_splits_series(self) -> None:
        splits_s = pd.Series(
            [4.0, 2.0],
            index=pd.to_datetime(["2020-08-31", "2014-06-09"]),
        )

        recs = parse_splits_series(
            splits_series=splits_s,
            sec_id="SEC_AAPL_001",
            ingestion_seq=1,
            start_date=date(2020, 1, 1),
        )

        assert len(recs) == 1  # 2014 filtered out by start_date
        assert recs[0]["action_type"] == "SPLIT"
        assert recs[0]["split_ratio"] == 4.0
        assert recs[0]["ex_date"] == date(2020, 8, 31)

    def test_parse_dividends_series(self) -> None:
        divs_s = pd.Series(
            [0.82, 0.22],
            index=pd.to_datetime(["2020-08-07", "2021-05-07"]),
        )

        recs = parse_dividends_series(
            divs_series=divs_s,
            sec_id="SEC_AAPL_001",
            ingestion_seq=1,
        )

        assert len(recs) == 2
        assert recs[0]["action_type"] == "CASH_DIVIDEND"
        assert recs[0]["cash_amount"] == 0.82
        assert recs[0]["split_ratio"] is None


class TestIngestionPipelinesWithMocks:
    """Tests for end-to-end ingestion pipelines with mocked network responses."""

    @patch("ledger.ingestion.market_data.yf.Ticker")
    def test_ingest_ohlcv_mocked(
        self,
        mock_ticker_cls: MagicMock,
        temp_raw_dir: Path,
    ) -> None:
        mock_pdf = pd.DataFrame(
            {
                "Open": [500.0],
                "High": [510.0],
                "Low": [495.0],
                "Close": [505.0],
                "Adj Close": [480.0],
                "Volume": [20000000],
            },
            index=pd.to_datetime(["2020-08-28"]),
        )
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = mock_pdf
        mock_ticker_cls.return_value = mock_ticker

        entry = ingest_ohlcv(
            tickers=["AAPL"],
            start_date="2020-08-28",
            end_date="2020-08-29",
            base_dir=temp_raw_dir,
        )

        assert entry is not None
        assert entry.ingestion_seq >= 1
        assert entry.row_count == 1
        assert entry.table_name == "market_ohlcv"

        # Verify DuckDB catalog can query the written partition
        catalog = LedgerCatalog(base_dir=temp_raw_dir)
        res = catalog.query("SELECT sec_id, trade_date, close FROM fact_market_ohlcv_raw")
        assert len(res) == 1
        assert res["close"][0] == 505.0
        catalog.close()

    @patch("ledger.ingestion.corporate_actions.yf.Ticker")
    def test_ingest_corporate_actions_mocked(
        self,
        mock_ticker_cls: MagicMock,
        temp_raw_dir: Path,
    ) -> None:
        mock_ticker = MagicMock()
        mock_ticker.splits = pd.Series([4.0], index=pd.to_datetime(["2020-08-31"]))
        mock_ticker.dividends = pd.Series([0.82], index=pd.to_datetime(["2020-08-07"]))
        mock_ticker_cls.return_value = mock_ticker

        entry = ingest_corporate_actions(
            tickers=["AAPL"],
            start_date="2020-01-01",
            end_date="2020-12-31",
            base_dir=temp_raw_dir,
        )

        assert entry is not None
        assert entry.row_count == 2  # 1 split + 1 dividend
        assert entry.table_name == "corporate_actions"

        catalog = LedgerCatalog(base_dir=temp_raw_dir)
        ca_res = catalog.query(
            """
            SELECT action_type, split_ratio, cash_amount
            FROM fact_corporate_actions
            ORDER BY action_type
            """
        )
        assert len(ca_res) == 2
        catalog.close()
