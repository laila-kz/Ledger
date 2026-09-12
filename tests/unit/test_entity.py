"""Unit tests for entity resolution and bitemporal ticker mapping."""

from datetime import date, datetime

import polars as pl
import pytest

from ledger.core.entity import (
    SecurityTickerMapping,
    create_ticker_map_view_sql,
    register_ticker_map_view,
    resolve_sec_id,
    resolve_ticker,
)


@pytest.fixture
def ticker_mapping_df() -> pl.DataFrame:
    """Fixture containing raw ticker mappings including renames and ticker reuses."""
    return pl.DataFrame(
        {
            "sec_id": [
                "SEC_META",
                "SEC_META",
                "SEC_GOOGL",
                "SEC_GOOGL",
                "SEC_OLD_CORP",
                "SEC_NEW_CORP",
            ],
            "ticker": [
                "FB",
                "META",
                "GOOG",
                "GOOGL",
                "XYZ",
                "XYZ",
            ],
            "valid_from": [
                datetime(2012, 5, 18, 0, 0),
                datetime(2022, 6, 9, 0, 0),
                datetime(2004, 8, 19, 0, 0),
                datetime(2014, 4, 2, 0, 0),
                datetime(2010, 1, 1, 0, 0),
                datetime(2020, 1, 1, 0, 0),
            ],
            "ingestion_seq": [1, 2, 1, 2, 1, 1],
        }
    )


class TestSecurityTickerMappingModel:
    """Tests for SecurityTickerMapping Pydantic model validation."""

    def test_model_instantiation(self) -> None:
        m = SecurityTickerMapping(
            sec_id="SEC_AAPL",
            ticker="AAPL",
            valid_from=date(1980, 12, 12),
        )
        assert m.sec_id == "SEC_AAPL"
        assert m.ticker == "AAPL"
        assert m.valid_from == date(1980, 12, 12)
        assert m.ingestion_seq == 1


class TestEntityResolutionPolars:
    """Tests for resolve_sec_id and resolve_ticker using Polars DataFrame."""

    def test_ticker_rename_fb_meta(self, ticker_mapping_df: pl.DataFrame) -> None:
        # Pre-rename (2020): FB resolves to SEC_META, META does not exist
        assert resolve_sec_id("FB", "2020-01-01", df=ticker_mapping_df) == "SEC_META"
        assert resolve_sec_id("META", "2020-01-01", df=ticker_mapping_df) is None

        # Post-rename (2023): META resolves to SEC_META, FB is no longer active
        assert resolve_sec_id("META", "2023-01-01", df=ticker_mapping_df) == "SEC_META"
        assert resolve_sec_id("FB", "2023-01-01", df=ticker_mapping_df) is None

        # Both point to the exact same SecID across time
        sec_fb = resolve_sec_id("FB", "2020-01-01", df=ticker_mapping_df)
        sec_meta = resolve_sec_id("META", "2023-01-01", df=ticker_mapping_df)
        assert sec_fb == sec_meta == "SEC_META"

    def test_rename_exact_boundary(self, ticker_mapping_df: pl.DataFrame) -> None:
        # Day before rename (2022-06-08)
        assert resolve_sec_id("FB", "2022-06-08", df=ticker_mapping_df) == "SEC_META"
        assert resolve_sec_id("META", "2022-06-08", df=ticker_mapping_df) is None

        # Effective date of rename (2022-06-09)
        assert resolve_sec_id("META", "2022-06-09", df=ticker_mapping_df) == "SEC_META"
        assert resolve_sec_id("FB", "2022-06-09", df=ticker_mapping_df) is None

    def test_ticker_reuse_disjoint_entities(self, ticker_mapping_df: pl.DataFrame) -> None:
        # Old corporation in 2012
        assert resolve_sec_id("XYZ", "2012-06-01", df=ticker_mapping_df) == "SEC_OLD_CORP"

        # New corporation in 2022
        assert resolve_sec_id("XYZ", "2022-06-01", df=ticker_mapping_df) == "SEC_NEW_CORP"

    def test_inverse_resolve_ticker(self, ticker_mapping_df: pl.DataFrame) -> None:
        # Inverse: SecID -> active ticker as of date
        assert resolve_ticker("SEC_META", "2020-01-01", df=ticker_mapping_df) == "FB"
        assert resolve_ticker("SEC_META", "2023-01-01", df=ticker_mapping_df) == "META"
        assert resolve_ticker("SEC_NONEXISTENT", "2023-01-01", df=ticker_mapping_df) is None

    def test_case_insensitivity_and_whitespace(self, ticker_mapping_df: pl.DataFrame) -> None:
        assert resolve_sec_id("  fb  ", "2020-01-01", df=ticker_mapping_df) == "SEC_META"
        assert resolve_sec_id("meta", "2023-01-01", df=ticker_mapping_df) == "SEC_META"


class TestEntityResolutionDuckDB:
    """Tests for entity resolution when querying via DuckDB connection and SQL views."""

    def test_duckdb_view_and_resolution(self) -> None:
        try:
            import duckdb
        except ImportError:
            pytest.skip("DuckDB not installed in environment yet.")

        conn = duckdb.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE dim_security_ticker_raw (
                sec_id VARCHAR NOT NULL,
                ticker VARCHAR NOT NULL,
                valid_from TIMESTAMP NOT NULL,
                ingestion_seq BIGINT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO dim_security_ticker_raw VALUES
                ('SEC_META', 'FB', TIMESTAMP '2012-05-18 00:00:00', 1),
                ('SEC_META', 'META', TIMESTAMP '2022-06-09 00:00:00', 2);
            """
        )

        register_ticker_map_view(conn)

        # Query FB in 2020 and META in 2023
        assert resolve_sec_id("FB", "2020-01-01", conn=conn) == "SEC_META"
        assert resolve_sec_id("META", "2023-01-01", conn=conn) == "SEC_META"
        assert resolve_sec_id("FB", "2023-01-01", conn=conn) is None
        assert resolve_sec_id("META", "2020-01-01", conn=conn) is None

        # Inverse resolve
        assert resolve_ticker("SEC_META", "2020-01-01", conn=conn) == "FB"
        assert resolve_ticker("SEC_META", "2023-01-01", conn=conn) == "META"

    def test_sql_generation(self) -> None:
        sql = create_ticker_map_view_sql("my_raw_table", "my_view")
        assert "CREATE OR REPLACE VIEW my_view" in sql
        assert "FROM my_raw_table" in sql
        assert "LEAD(valid_from) OVER" in sql
