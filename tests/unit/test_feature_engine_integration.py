"""End-to-end integration tests for the complete Declarative Feature Store pipeline.

Validates:
1. Declarative registry DAG resolution (`adj_close` -> `[momentum, volatility, SMA, EMA]`).
2. Point-in-time ASOF feature computation via `compute_features_as_of()`.
3. Invariant enforcement: Zero look-ahead across trade date publication cutoffs.
4. Dynamic CAF split-awareness end-to-end across observation matrix evaluations.
5. DuckDB catalog connection integration.
"""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl
import pytest

from ledger.features.engine import compute_features_as_of
from ledger.storage.catalog import LedgerCatalog

UTC = zoneinfo.ZoneInfo("UTC")


# =============================================================================
# 1. End-to-End Multi-Feature Extraction
# =============================================================================


class TestEndToEndFeaturePipeline:
    """Tests the full flow from raw inputs to joined observation matrix."""

    def test_compute_multiple_technical_features(self) -> None:
        num_days = 60
        base_date = date(2023, 1, 1)
        base_dt = datetime(2023, 1, 1, 21, 0, tzinfo=UTC)

        prices = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"] * num_days,
                "trade_date": [base_date + timedelta(days=i) for i in range(num_days)],
                "close": [100.0 + i for i in range(num_days)],
                "known_from": [base_dt + timedelta(days=i) for i in range(num_days)],
            }
        )

        # Query on day 55 at 21:05 UTC (after day 55 bar published)
        entity_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"],
                "observation_timestamp": [base_dt + timedelta(days=55, minutes=5)],
            }
        )

        features_to_extract = ["momentum_20d", "volatility_20d", "sma_50d", "ema_50d"]

        joined = compute_features_as_of(
            entity_df=entity_df,
            feature_names=features_to_extract,
            prices=prices,
        )

        expected_cols = [
            "sec_id",
            "observation_timestamp",
            "momentum_20d",
            "volatility_20d",
            "sma_50d",
            "ema_50d",
        ]
        assert joined.columns == expected_cols
        assert joined.height == 1

        # Assert values are populated (non-null after 50+ day warm-up)
        assert joined["momentum_20d"][0] is not None
        assert joined["volatility_20d"][0] is not None
        assert joined["sma_50d"][0] is not None
        assert joined["ema_50d"][0] is not None

        # Momentum at day 55: (close[55] / close[35]) - 1 = (155.0 / 135.0) - 1.0
        assert joined["momentum_20d"][0] == pytest.approx((155.0 / 135.0) - 1.0)


# =============================================================================
# 2. Zero Look-Ahead Publication Cutoff Invariant
# =============================================================================


class TestZeroLookAheadPublicationCutoffs:
    """Verifies features respect intraday publication cutoffs."""

    def test_observation_before_and_after_market_close(self) -> None:
        num_days = 30
        base_date = date(2023, 1, 1)
        base_dt = datetime(2023, 1, 1, 21, 0, tzinfo=UTC)

        prices = pl.DataFrame(
            {
                "sec_id": ["SEC_1"] * num_days,
                "trade_date": [base_date + timedelta(days=i) for i in range(num_days)],
                "close": [100.0 + i for i in range(num_days)],
                "known_from": [base_dt + timedelta(days=i) for i in range(num_days)],
            }
        )

        # Day 25:
        # 1. Observation at 15:00 UTC (pre-close 21:00 UTC) -> must see day 24 momentum
        # 2. Observation at 21:05 UTC (post-close) -> must see day 25 momentum
        entity_df = pl.DataFrame(
            {
                "sec_id": ["SEC_1", "SEC_1"],
                "observation_timestamp": [
                    datetime(2023, 1, 26, 15, 0, tzinfo=UTC),  # Day 25 at 15:00
                    datetime(2023, 1, 26, 21, 5, tzinfo=UTC),  # Day 25 at 21:05
                ],
            }
        )

        joined = compute_features_as_of(
            entity_df=entity_df,
            feature_names=["momentum_20d"],
            prices=prices,
        )

        # Observation 1 (15:00 UTC): Day 24 momentum = (124 / 104) - 1
        expected_mom_day24 = (124.0 / 104.0) - 1.0
        # Observation 2 (21:05 UTC): Day 25 momentum = (125 / 105) - 1
        expected_mom_day25 = (125.0 / 105.0) - 1.0

        assert joined["momentum_20d"][0] == pytest.approx(expected_mom_day24)
        assert joined["momentum_20d"][1] == pytest.approx(expected_mom_day25)


# =============================================================================
# 3. Dynamic CAF Split-Awareness End-to-End
# =============================================================================


class TestDynamicCAFSplitAwarenessEndToEnd:
    """Verifies end-to-end feature extraction across stock split events."""

    def test_end_to_end_split_adjusted_momentum(self) -> None:
        num_days = 35
        base_date = date(2020, 8, 1)
        base_dt = datetime(2020, 8, 1, 21, 0, tzinfo=UTC)

        # 4:1 split on 2020-08-16 (day 15)
        # Constant economic price $100 (raw $25 pre-split, raw $100 post-split)
        raw_closes = [25.0] * 15 + [100.0] * 20
        prices = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"] * num_days,
                "trade_date": [base_date + timedelta(days=i) for i in range(num_days)],
                "close": raw_closes,
                "known_from": [base_dt + timedelta(days=i) for i in range(num_days)],
            }
        )
        splits = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"],
                "ex_date": [base_date + timedelta(days=15)],
                "split_ratio": [4.0],
                "known_from": [base_dt + timedelta(days=15)],
            }
        )

        # Query on day 25 (post-split, spanning the split window)
        entity_df = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"],
                "observation_timestamp": [base_dt + timedelta(days=25, minutes=10)],
            }
        )

        joined = compute_features_as_of(
            entity_df=entity_df,
            feature_names=["momentum_20d", "volatility_20d"],
            prices=prices,
            splits=splits,
        )

        # With proper dynamic CAF, momentum is 0.0 and volatility is 0.0 across the split
        assert joined["momentum_20d"][0] == pytest.approx(0.0)
        assert joined["volatility_20d"][0] == pytest.approx(0.0)


# =============================================================================
# 4. Catalog Integration
# =============================================================================


class TestCatalogFeatureComputation:
    """Tests compute_features_as_of using a DuckDB LedgerCatalog."""

    def test_compute_features_via_catalog(self, tmp_path: Any) -> None:
        catalog = LedgerCatalog(base_dir=tmp_path / "raw")
        conn = catalog.get_connection()

        # Seed catalog table via REPLACE VIEW
        conn.execute("""
        CREATE OR REPLACE VIEW fact_market_ohlcv_raw AS
        SELECT
            'SEC_MSFT_001' AS sec_id,
            CAST('2023-01-01' AS DATE) + INTERVAL (i) DAY AS trade_date,
            100.0 + i AS open,
            102.0 + i AS high,
            99.0 + i AS low,
            100.0 + i AS close,
            1000000 AS volume,
            TIMESTAMPTZ '2023-01-01 21:00:00Z' + INTERVAL (i) DAY AS known_from,
            CAST(i + 1 AS BIGINT) AS ingestion_seq
        FROM range(30) t(i)
        """)

        conn.execute("""
        CREATE OR REPLACE VIEW fact_corporate_actions AS
        SELECT
            CAST(NULL AS VARCHAR) AS sec_id,
            CAST(NULL AS VARCHAR) AS action_type,
            CAST(NULL AS DATE) AS ex_date,
            CAST(NULL AS DOUBLE) AS split_ratio,
            CAST(NULL AS DOUBLE) AS cash_amount,
            CAST(NULL AS DATE) AS announcement_date,
            CAST(NULL AS TIMESTAMP) AS known_from,
            CAST(NULL AS BIGINT) AS ingestion_seq
        WHERE 1 = 0
        """)

        entity_df = pl.DataFrame(
            {
                "sec_id": ["SEC_MSFT_001"],
                "observation_timestamp": [datetime(2023, 1, 26, 21, 5, tzinfo=UTC)],
            }
        )

        joined = compute_features_as_of(
            entity_df=entity_df,
            feature_names=["momentum_20d"],
            catalog=catalog,
        )

        assert "momentum_20d" in joined.columns
        assert joined["momentum_20d"][0] is not None
