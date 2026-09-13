"""Unit tests for the vectorized Point-in-Time (PIT) ASOF join engine.

Covers:
1. Schema validation and error handling for ObservationMatrix.
2. Single-feature and multi-feature point-in-time joins.
3. Ticker re-identification scenario (FB -> META on 2022-06-09).
4. SEC filing lag window isolation (known_from availability guard).
5. Derived known_to / valid_to bound invalidation and restatements.
6. DuckDB catalog string view resolution.
7. Vectorized performance benchmark (< 100ms for 1,000+ observations).
"""

from __future__ import annotations

import time
import zoneinfo
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl
import pytest
from pydantic import ValidationError

from ledger.core.bitemporal import derive_known_to_polars, derive_valid_to_polars
from ledger.features.engine import (
    ObservationMatrix,
    join_features_as_of,
    validate_observation_matrix,
)
from ledger.storage.catalog import LedgerCatalog

UTC = zoneinfo.ZoneInfo("UTC")


# =============================================================================
# 1. Observation Matrix Schema & Validation Tests
# =============================================================================


class TestObservationMatrixSchema:
    """Tests for ObservationMatrix model and DataFrame schema validation."""

    def test_pydantic_model_valid(self) -> None:
        obs = ObservationMatrix(
            sec_id="SEC_AAPL_001",
            observation_timestamp=datetime(2023, 1, 15, 21, 0, tzinfo=UTC),
        )
        assert obs.sec_id == "SEC_AAPL_001"
        assert obs.observation_timestamp == datetime(2023, 1, 15, 21, 0, tzinfo=UTC)

    def test_pydantic_model_invalid_missing_fields(self) -> None:
        with pytest.raises(ValidationError):
            ObservationMatrix.model_validate({"sec_id": "SEC_AAPL_001"})

    def test_validate_observation_matrix_valid_types(self) -> None:
        df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001", "SEC_MSFT_001"],
                "observation_timestamp": [
                    datetime(2023, 1, 15, 21, 0, tzinfo=UTC),
                    datetime(2023, 1, 16, 21, 0, tzinfo=UTC),
                ],
            }
        )
        validated = validate_observation_matrix(df)
        assert validated.shape == (2, 2)
        assert validated["sec_id"].dtype == pl.String
        assert isinstance(validated["observation_timestamp"].dtype, pl.Datetime)
        assert validated["observation_timestamp"].dtype.time_zone == "UTC"

    def test_validate_observation_matrix_date_cast(self) -> None:
        df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"],
                "observation_timestamp": [date(2023, 1, 15)],
            }
        )
        validated = validate_observation_matrix(df)
        assert validated["observation_timestamp"].dtype == pl.Datetime("us", "UTC")

    def test_validate_observation_matrix_missing_columns_raise(self) -> None:
        df = pl.DataFrame({"ticker": ["AAPL"], "date": [date(2023, 1, 15)]})
        with pytest.raises(ValueError, match="missing mandatory column"):
            validate_observation_matrix(df)

    def test_validate_observation_matrix_nulls_raise(self) -> None:
        df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001", None],
                "observation_timestamp": [
                    datetime(2023, 1, 15, 21, 0, tzinfo=UTC),
                    datetime(2023, 1, 16, 21, 0, tzinfo=UTC),
                ],
            }
        )
        with pytest.raises(ValueError, match="contains null values"):
            validate_observation_matrix(df)

    def test_validate_observation_matrix_non_df_raise(self) -> None:
        with pytest.raises(TypeError, match="must be a polars.DataFrame"):
            validate_observation_matrix({"sec_id": "SEC_1"})  # type: ignore[arg-type]


# =============================================================================
# 2. Point-in-Time Join Mechanics & Invariant Tests
# =============================================================================


class TestPointInTimeJoinMechanics:
    """Tests for core backward ASOF join and zero look-ahead availability."""

    def test_basic_eod_price_asof_join(self) -> None:
        prices_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001", "SEC_AAPL_001", "SEC_AAPL_001"],
                "trade_date": [date(2023, 1, 3), date(2023, 1, 4), date(2023, 1, 5)],
                "close": [130.0, 132.0, 135.0],
                "known_from": [
                    datetime(2023, 1, 3, 21, 0, tzinfo=UTC),
                    datetime(2023, 1, 4, 21, 0, tzinfo=UTC),
                    datetime(2023, 1, 5, 21, 0, tzinfo=UTC),
                ],
            }
        )

        # Query at Jan 4 15:00 UTC (before Jan 4 close published) -> must see Jan 3 close (130.0)
        # Query at Jan 4 21:05 UTC (after Jan 4 close published) -> must see Jan 4 close (132.0)
        entity_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001", "SEC_AAPL_001"],
                "observation_timestamp": [
                    datetime(2023, 1, 4, 15, 0, tzinfo=UTC),
                    datetime(2023, 1, 4, 21, 5, tzinfo=UTC),
                ],
            }
        )

        joined = join_features_as_of(entity_df, [prices_df])
        assert joined["close"].to_list() == [130.0, 132.0]

    def test_observation_prior_to_any_known_fact_yields_null(self) -> None:
        prices_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"],
                "close": [150.0],
                "known_from": [datetime(2023, 1, 10, 21, 0, tzinfo=UTC)],
            }
        )
        entity_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"],
                "observation_timestamp": [datetime(2023, 1, 5, 12, 0, tzinfo=UTC)],
            }
        )

        joined = join_features_as_of(entity_df, [prices_df])
        assert joined["close"][0] is None

    def test_multi_security_isolation(self) -> None:
        prices_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001", "SEC_MSFT_001"],
                "close": [150.0, 240.0],
                "known_from": [
                    datetime(2023, 1, 3, 21, 0, tzinfo=UTC),
                    datetime(2023, 1, 3, 21, 0, tzinfo=UTC),
                ],
            }
        )
        entity_df = pl.DataFrame(
            {
                "sec_id": ["SEC_MSFT_001", "SEC_AAPL_001"],
                "observation_timestamp": [
                    datetime(2023, 1, 4, 9, 30, tzinfo=UTC),
                    datetime(2023, 1, 4, 9, 30, tzinfo=UTC),
                ],
            }
        )

        joined = join_features_as_of(entity_df, [prices_df])
        assert joined["close"].to_list() == [240.0, 150.0]


# =============================================================================
# 3. Canary Scenario: FB -> META Ticker Re-identification
# =============================================================================


class TestTickerReidentificationFBToMeta:
    """Canary 06 test: Verifies SecID-based bitemporal mapping across FB -> META rename."""

    def test_fb_to_meta_point_in_time_resolution(self) -> None:
        raw_entity_map = pl.DataFrame(
            {
                "sec_id": ["SEC_META_001", "SEC_META_001"],
                "ticker": ["FB", "META"],
                "valid_from": [
                    datetime(2012, 5, 18, 0, 0, tzinfo=UTC),
                    datetime(2022, 6, 9, 0, 0, tzinfo=UTC),
                ],
                "ingestion_seq": [1, 2],
            }
        )
        # Derive valid_to: FB valid_to = 2022-06-09, META valid_to = None
        ticker_view = derive_valid_to_polars(raw_entity_map, partition_by="sec_id")

        # Query points:
        # 1. 2020-01-01 -> "FB"
        # 2. 2022-06-08 23:59:59 -> "FB"
        # 3. 2022-06-09 00:00:00 -> "META"
        # 4. 2023-01-01 -> "META"
        # 5. 2010-01-01 (pre-IPO) -> None
        entity_df = pl.DataFrame(
            {
                "sec_id": [
                    "SEC_META_001",
                    "SEC_META_001",
                    "SEC_META_001",
                    "SEC_META_001",
                    "SEC_META_001",
                ],
                "observation_timestamp": [
                    datetime(2020, 1, 1, 12, 0, tzinfo=UTC),
                    datetime(2022, 6, 8, 23, 59, 59, tzinfo=UTC),
                    datetime(2022, 6, 9, 0, 0, 0, tzinfo=UTC),
                    datetime(2023, 1, 1, 12, 0, tzinfo=UTC),
                    datetime(2010, 1, 1, 0, 0, tzinfo=UTC),
                ],
            }
        )

        joined = join_features_as_of(entity_df, [ticker_view])
        expected_tickers = ["FB", "FB", "META", "META", None]
        assert joined["ticker"].to_list() == expected_tickers


# =============================================================================
# 4. Canary Scenario: SEC Filing Lag Window (Canary 05)
# =============================================================================


class TestFilingLagWindow:
    """Canary 05 test: Fiscal quarter ends March 31, but 10-Q is filed May 10."""

    def test_q1_filing_lag_window_isolation(self) -> None:
        fundamentals_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001", "SEC_AAPL_001"],
                "fiscal_period_end": [date(2021, 12, 31), date(2022, 3, 31)],
                "eps_diluted": [2.10, 1.52],
                "known_from": [
                    datetime(2022, 1, 28, 16, 30, tzinfo=UTC),  # Q4 filed late Jan
                    datetime(2022, 5, 10, 16, 30, tzinfo=UTC),  # Q1 filed May 10
                ],
            }
        )

        # Observations:
        # 1. 2022-04-15 (during filing lag): must see Q4 EPS (2.10), NOT Q1 (1.52)
        # 2. 2022-05-10 16:29:59 (1 second before filing): must see Q4 EPS (2.10)
        # 3. 2022-05-10 16:30:00 (exact filing time): must see Q1 EPS (1.52)
        # 4. 2022-05-15 (post-filing): must see Q1 EPS (1.52)
        entity_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001", "SEC_AAPL_001", "SEC_AAPL_001", "SEC_AAPL_001"],
                "observation_timestamp": [
                    datetime(2022, 4, 15, 12, 0, tzinfo=UTC),
                    datetime(2022, 5, 10, 16, 29, 59, tzinfo=UTC),
                    datetime(2022, 5, 10, 16, 30, 0, tzinfo=UTC),
                    datetime(2022, 5, 15, 12, 0, tzinfo=UTC),
                ],
            }
        )

        joined = join_features_as_of(entity_df, [fundamentals_df])
        assert joined["eps_diluted"].to_list() == [2.10, 2.10, 1.52, 1.52]


# =============================================================================
# 5. Canary Scenario: Restatement with Derived known_to (Canary 01)
# =============================================================================


class TestRestatedFundamentalsBoundEnforcement:
    """Canary 01 test: Q2 EPS filed in Aug ($1.00), restated in Nov ($0.70)."""

    def test_restatement_point_in_time_switch(self) -> None:
        raw_filings = pl.DataFrame(
            {
                "sec_id": ["SEC_CO_001", "SEC_CO_001"],
                "metric_name": ["eps", "eps"],
                "metric_value": [1.00, 0.70],
                "known_from": [
                    datetime(2022, 8, 10, 16, 0, tzinfo=UTC),
                    datetime(2022, 11, 5, 16, 0, tzinfo=UTC),
                ],
                "ingestion_seq": [1, 2],
            }
        )
        # Derive known_to: row 1 known_to = 2022-11-05 16:00, row 2 known_to = None
        bitemporal_view = derive_known_to_polars(
            raw_filings, partition_by=["sec_id", "metric_name"]
        )

        entity_df = pl.DataFrame(
            {
                "sec_id": ["SEC_CO_001", "SEC_CO_001", "SEC_CO_001"],
                "observation_timestamp": [
                    datetime(2022, 9, 1, 12, 0, tzinfo=UTC),  # Before restatement -> 1.00
                    datetime(2022, 11, 5, 15, 59, tzinfo=UTC),  # 1 min before -> 1.00
                    datetime(2022, 11, 5, 16, 0, tzinfo=UTC),  # At restatement -> 0.70
                ],
            }
        )

        joined = join_features_as_of(entity_df, [bitemporal_view])
        assert joined["metric_value"].to_list() == [1.00, 1.00, 0.70]

    def test_expired_fact_without_successor_is_nullified(self) -> None:
        # Fact active only from Jan 1 to March 1
        fact_df = pl.DataFrame(
            {
                "sec_id": ["SEC_TEMP_001"],
                "score": [99.0],
                "known_from": [datetime(2023, 1, 1, 0, 0, tzinfo=UTC)],
                "known_to": [datetime(2023, 3, 1, 0, 0, tzinfo=UTC)],
            }
        )

        entity_df = pl.DataFrame(
            {
                "sec_id": ["SEC_TEMP_001", "SEC_TEMP_001"],
                "observation_timestamp": [
                    datetime(2023, 2, 1, 0, 0, tzinfo=UTC),  # Within window -> 99.0
                    datetime(2023, 3, 1, 0, 0, tzinfo=UTC),  # Expired -> None
                ],
            }
        )

        joined = join_features_as_of(entity_df, [fact_df])
        assert joined["score"].to_list() == [99.0, None]


# =============================================================================
# 6. Multi-Feature Sequential Joins & Catalog Integration
# =============================================================================


class TestMultiFeatureJoinsAndCatalog:
    """Tests joining multiple feature views simultaneously and DuckDB catalog resolution."""

    def test_multi_feature_simultaneous_join(self) -> None:
        # Feature View 1: Prices
        prices_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"],
                "close": [150.0],
                "volume": [1_000_000],
                "known_from": [datetime(2023, 1, 3, 21, 0, tzinfo=UTC)],
            }
        )

        # Feature View 2: Ticker Map
        ticker_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"],
                "ticker": ["AAPL"],
                "valid_from": [datetime(2020, 1, 1, 0, 0, tzinfo=UTC)],
            }
        )

        # Feature View 3: Fundamentals
        fund_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"],
                "pe_ratio": [25.5],
                "known_from": [datetime(2023, 1, 1, 0, 0, tzinfo=UTC)],
            }
        )

        entity_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"],
                "observation_timestamp": [datetime(2023, 1, 4, 12, 0, tzinfo=UTC)],
            }
        )

        joined = join_features_as_of(
            entity_df,
            feature_views=[prices_df, ticker_df, fund_df],
        )

        expected_cols = [
            "sec_id",
            "observation_timestamp",
            "close",
            "volume",
            "ticker",
            "pe_ratio",
        ]
        assert joined.columns == expected_cols
        assert joined["close"][0] == 150.0
        assert joined["volume"][0] == 1_000_000
        assert joined["ticker"][0] == "AAPL"
        assert joined["pe_ratio"][0] == 25.5

    def test_duckdb_catalog_view_resolution(self, tmp_path: Any) -> None:
        catalog = LedgerCatalog(base_dir=tmp_path / "raw")
        conn = catalog.get_connection()

        # Seed in-memory table in DuckDB
        conn.execute("""
        CREATE TABLE test_technical_view AS
        SELECT
            'SEC_AAPL_001' AS sec_id,
            155.0 AS rsi_14,
            TIMESTAMPTZ '2023-01-03 21:00:00Z' AS known_from
        """)

        entity_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"],
                "observation_timestamp": [datetime(2023, 1, 4, 12, 0, tzinfo=UTC)],
            }
        )

        joined = join_features_as_of(
            entity_df,
            feature_views=["test_technical_view"],
            catalog=catalog,
        )

        assert "rsi_14" in joined.columns
        assert joined["rsi_14"][0] == 155.0

    def test_polars_duckdb_parity_identical_results(self, tmp_path: Any) -> None:
        """Assert identical output between direct Polars DataFrame and DuckDB catalog view paths."""
        catalog = LedgerCatalog(base_dir=tmp_path / "raw")
        conn = catalog.get_connection()

        feature_data = {
            "sec_id": ["SEC_AAPL_001", "SEC_MSFT_001", "SEC_AAPL_001"],
            "momentum": [0.05, -0.02, 0.08],
            "known_from": [
                datetime(2023, 1, 2, 21, 0, tzinfo=UTC),
                datetime(2023, 1, 2, 21, 0, tzinfo=UTC),
                datetime(2023, 1, 3, 21, 0, tzinfo=UTC),
            ],
        }
        polars_df = pl.DataFrame(feature_data)

        conn.execute("""
        CREATE TABLE parity_view AS
        SELECT
            sec_id,
            momentum,
            known_from
        FROM polars_df
        """)

        entity_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001", "SEC_AAPL_001", "SEC_MSFT_001"],
                "observation_timestamp": [
                    datetime(2023, 1, 3, 12, 0, tzinfo=UTC),
                    datetime(2023, 1, 4, 9, 30, tzinfo=UTC),
                    datetime(2023, 1, 3, 9, 30, tzinfo=UTC),
                ],
            }
        )

        res_polars = join_features_as_of(entity_df, [polars_df])
        res_duckdb = join_features_as_of(entity_df, ["parity_view"], catalog=catalog)

        from polars.testing import assert_frame_equal

        assert_frame_equal(res_polars, res_duckdb)

    def test_intraday_timestamp_granularity_backward_asof(self) -> None:
        """Assert backward ASOF matches previous row when query precedes afternoon filing."""
        filings_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001", "SEC_AAPL_001"],
                "eps": [1.00, 1.50],
                "known_from": [
                    datetime(2022, 5, 9, 21, 0, tzinfo=UTC),  # Day 1 EOD
                    datetime(2022, 5, 10, 17, 30, tzinfo=UTC),  # Day 2 Afternoon (17:30)
                ],
            }
        )

        # Observation on Day 2 morning (09:00 UTC) must see Day 1 EPS (1.00), not Day 2 (1.50)
        # Observation on Day 2 evening (18:00 UTC) must see Day 2 EPS (1.50)
        entity_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001", "SEC_AAPL_001"],
                "observation_timestamp": [
                    datetime(2022, 5, 10, 9, 0, tzinfo=UTC),
                    datetime(2022, 5, 10, 18, 0, tzinfo=UTC),
                ],
            }
        )

        joined = join_features_as_of(entity_df, [filings_df])
        assert joined["eps"].to_list() == [1.00, 1.50]

    def test_ambiguous_keys_raise_error(self) -> None:
        """Assert ValueError is raised when known_from and valid_from exist without right_on."""
        ambiguous_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"],
                "val": [10.0],
                "known_from": [datetime(2023, 1, 1, 0, 0, tzinfo=UTC)],
                "valid_from": [datetime(2023, 1, 1, 0, 0, tzinfo=UTC)],
            }
        )
        entity_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"],
                "observation_timestamp": [datetime(2023, 1, 2, 0, 0, tzinfo=UTC)],
            }
        )

        with pytest.raises(ValueError, match="Ambiguous temporal keys"):
            join_features_as_of(entity_df, [ambiguous_df])


# =============================================================================
# 7. Performance & Scalability Benchmark
# =============================================================================


class TestVectorizedEnginePerformance:
    """Benchmark asserting vectorized ASOF join runs in < 100ms on 1000+ observations."""

    def test_benchmark_1000_observations_sub_100ms(self) -> None:
        num_tickers = 5
        num_obs_per_ticker = 300  # 1,500 total observations
        sec_ids = [f"SEC_{i:03d}" for i in range(num_tickers)]

        base_date = datetime(2022, 1, 1, 21, 0, tzinfo=UTC)

        # Build feature price table (1,000 daily bars per ticker = 5,000 rows)
        feature_rows: list[dict[str, Any]] = []
        for sec in sec_ids:
            for day in range(1000):
                feature_rows.append(
                    {
                        "sec_id": sec,
                        "close": 100.0 + day * 0.1,
                        "known_from": base_date + timedelta(days=day),
                    }
                )
        feature_df = pl.DataFrame(feature_rows)

        # Build observation matrix (1,500 observation queries)
        obs_rows: list[dict[str, Any]] = []
        for sec in sec_ids:
            for i in range(num_obs_per_ticker):
                obs_rows.append(
                    {
                        "sec_id": sec,
                        "observation_timestamp": base_date + timedelta(days=i * 3, hours=10),
                    }
                )
        entity_df = pl.DataFrame(obs_rows)

        assert entity_df.height == 1500

        # Execute and benchmark
        start_time = time.perf_counter()
        joined = join_features_as_of(entity_df, [feature_df])
        duration_ms = (time.perf_counter() - start_time) * 1000

        assert joined.height == 1500
        assert "close" in joined.columns
        assert joined["close"].null_count() == 0

        # Performance Assertion: must execute in under 100ms
        assert duration_ms < 100.0, f"Engine exceeded 100ms latency budget: {duration_ms:.2f}ms"


# =============================================================================
# 8. Zero-Memory-Copy Arrow Sharing Verification
# =============================================================================


class TestZeroCopyMemoryTransfer:
    """Verifies Arrow zero-copy memory interoperability between Polars and DuckDB."""

    def test_arrow_zero_copy_roundtrip_duckdb_polars(self) -> None:
        """Verify memory sharing between Polars and DuckDB via PyArrow without disk I/O."""
        import duckdb
        import pyarrow as pa

        # 1. Create Polars DataFrame
        num_rows = 50_000
        df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"] * num_rows,
                "price": [150.25 + i * 0.01 for i in range(num_rows)],
            }
        )

        # 2. Export to PyArrow Table (zero-copy memory view)
        arrow_table = df.to_arrow()
        assert isinstance(arrow_table, pa.Table)
        assert arrow_table.num_rows == num_rows

        # 3. Register table directly in in-memory DuckDB connection (no temp files / CSVs)
        con = duckdb.connect()
        con.register("in_memory_arrow_view", arrow_table)

        # 4. Query via DuckDB and extract Arrow stream directly
        result_arrow = con.execute(
            "SELECT sec_id, AVG(price) as avg_price, COUNT(*) as count FROM in_memory_arrow_view GROUP BY sec_id"
        ).arrow()

        # 5. Convert back to Polars from Arrow without serialization
        result_pl = pl.from_arrow(result_arrow)

        assert isinstance(result_pl, pl.DataFrame)
        assert result_pl.height == 1
        assert result_pl["count"][0] == num_rows
        assert result_pl["sec_id"][0] == "SEC_AAPL_001"
        assert result_pl["avg_price"][0] == pytest.approx(150.25 + (num_rows - 1) * 0.01 / 2.0)

