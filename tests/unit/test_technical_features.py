"""Unit tests for declarative technical features.

Covers: adj_close, momentum_20d, volatility_20d, sma_50d, ema_50d.

Verifies:
1. Mathematical precision for all technical indicators.
2. Warm-up boundary invariants (null count assertions).
3. Dynamic Cumulative Adjustment Factor (CAF) split-invariance across split boundaries.
4. Multi-security window isolation (preventing cross-security data bleeding).
"""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from ledger.features.definitions.adj_close import compute_adj_close
from ledger.features.definitions.technical import (
    compute_ema_50d,
    compute_momentum_20d,
    compute_sma_50d,
    compute_volatility_20d,
)
from ledger.features.registry import FeatureContext

UTC = zoneinfo.ZoneInfo("UTC")


# =============================================================================
# 1. Base Feature: adj_close Tests
# =============================================================================


class TestAdjustedCloseFeature:
    """Tests for the base adj_close feature definition."""

    def test_adj_close_without_splits(self) -> None:
        prices = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"] * 5,
                "trade_date": [date(2023, 1, i) for i in range(1, 6)],
                "close": [100.0, 102.0, 101.0, 103.0, 105.0],
                "known_from": [datetime(2023, 1, i, 21, 0, tzinfo=UTC) for i in range(1, 6)],
            }
        )
        ctx = FeatureContext(prices=prices)

        result = compute_adj_close(ctx)
        assert result.columns == ["sec_id", "trade_date", "close", "adj_close", "known_from"]
        assert result["adj_close"].to_list() == [100.0, 102.0, 101.0, 103.0, 105.0]

    def test_adj_close_with_4_for_1_split(self) -> None:
        # 4:1 split on 2020-08-31
        prices = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"] * 4,
                "trade_date": [
                    date(2020, 8, 27),
                    date(2020, 8, 28),
                    date(2020, 8, 31),
                    date(2020, 9, 1),
                ],
                "close": [500.0, 500.0, 125.0, 130.0],
                "known_from": [
                    datetime(2020, 8, 27, 21, 0, tzinfo=UTC),
                    datetime(2020, 8, 28, 21, 0, tzinfo=UTC),
                    datetime(2020, 8, 31, 21, 0, tzinfo=UTC),
                    datetime(2020, 9, 1, 21, 0, tzinfo=UTC),
                ],
            }
        )
        splits = pl.DataFrame(
            {
                "sec_id": ["SEC_AAPL_001"],
                "ex_date": [date(2020, 8, 31)],
                "split_ratio": [4.0],
                "known_from": [datetime(2020, 8, 31, 20, 15, tzinfo=UTC)],
            }
        )
        ctx = FeatureContext(prices=prices, splits=splits)

        result = compute_adj_close(ctx)
        # Pre-split 500.0 unadjusted becomes 500.0 * 0.25 = 125.0 in backward-adjusted terms
        # and 125.0 becomes 125.0 * 1.0 = 125.0
        assert result["adj_close"].to_list() == [125.0, 125.0, 125.0, 130.0]


# =============================================================================
# 2. Momentum 20-Day Feature Tests
# =============================================================================


class TestMomentum20dFeature:
    """Tests for 20-day momentum, warm-up boundaries, and split-invariance."""

    def test_momentum_20d_linear_trend_and_warmup(self) -> None:
        num_days = 25
        base_date = date(2023, 1, 1)
        base_dt = datetime(2023, 1, 1, 21, 0, tzinfo=UTC)

        prices = pl.DataFrame(
            {
                "sec_id": ["SEC_1"] * num_days,
                "trade_date": [base_date + timedelta(days=i) for i in range(num_days)],
                "close": [100.0 + i for i in range(num_days)],  # 100.0, 101.0, ... 124.0
                "known_from": [base_dt + timedelta(days=i) for i in range(num_days)],
            }
        )
        ctx = FeatureContext(prices=prices)

        result = compute_momentum_20d(ctx)
        assert result.columns == ["sec_id", "trade_date", "known_from", "momentum_20d"]

        # Warm-up check: First 20 values (indices 0..19) must be None
        assert result["momentum_20d"][:20].null_count() == 20

        # Observation at index 20 (21st trade day): close[20] = 120.0, close[0] = 100.0
        # Expected momentum = (120.0 / 100.0) - 1.0 = 0.20 (20%)
        assert result["momentum_20d"][20] == pytest.approx(0.20)
        # Observation at index 24: close[24] = 124.0, close[4] = 104.0
        assert result["momentum_20d"][24] == pytest.approx((124.0 / 104.0) - 1.0)

    def test_momentum_20d_split_invariance(self) -> None:
        """Assert momentum calculation is continuous across a 4:1 stock split."""
        num_days = 30
        base_date = date(2020, 8, 1)
        base_dt = datetime(2020, 8, 1, 21, 0, tzinfo=UTC)

        # Flat economic price of $100 equivalent:
        # Pre-split (days 0..14): raw unadjusted close = 400.0 (4x post-split)
        # Post-split (days 15..29): raw close = 100.0
        raw_closes = [400.0] * 15 + [100.0] * 15
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
        ctx = FeatureContext(prices=prices, splits=splits)

        result = compute_momentum_20d(ctx)

        # For days 20..29, the 20-day window spans across the split date (day 15).
        # Adjusted close for days 0..14 is 400.0 * 0.25 = 100.0
        # Adjusted close for days 15..29 is 100.0 * 1.0 = 100.0
        # Economic momentum across the split must be exactly 0.0 (flat), NOT +300% or -75%!
        for idx in range(20, 30):
            assert result["momentum_20d"][idx] == pytest.approx(0.0)


# =============================================================================
# 3. Volatility 20-Day Feature Tests
# =============================================================================


class TestVolatility20dFeature:
    """Tests for 20-day annualized realized volatility."""

    def test_volatility_20d_zero_for_flat_prices(self) -> None:
        num_days = 25
        base_date = date(2023, 1, 1)
        base_dt = datetime(2023, 1, 1, 21, 0, tzinfo=UTC)

        prices = pl.DataFrame(
            {
                "sec_id": ["SEC_1"] * num_days,
                "trade_date": [base_date + timedelta(days=i) for i in range(num_days)],
                "close": [100.0] * num_days,
                "known_from": [base_dt + timedelta(days=i) for i in range(num_days)],
            }
        )
        ctx = FeatureContext(prices=prices)

        result = compute_volatility_20d(ctx)
        assert result.columns == ["sec_id", "trade_date", "known_from", "volatility_20d"]

        # First 20 are None
        assert result["volatility_20d"][:20].null_count() == 20
        # All subsequent values are 0.0
        for idx in range(20, num_days):
            assert result["volatility_20d"][idx] == pytest.approx(0.0)

    def test_volatility_20d_split_invariance(self) -> None:
        """Assert a 4:1 split does not create an artificial volatility spike."""
        num_days = 30
        base_date = date(2020, 8, 1)
        base_dt = datetime(2020, 8, 1, 21, 0, tzinfo=UTC)

        # Pre-split raw 400.0, post-split raw 100.0 (constant economic price)
        raw_closes = [400.0] * 15 + [100.0] * 15
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
        ctx = FeatureContext(prices=prices, splits=splits)

        result = compute_volatility_20d(ctx)

        # Volatility across the split window (days 20..29) must remain 0.0
        for idx in range(20, 30):
            assert result["volatility_20d"][idx] == pytest.approx(0.0)


# =============================================================================
# 4. SMA 50-Day & EMA 50-Day Feature Tests
# =============================================================================


class TestMovingAveragesFeatures:
    """Tests for 50-day Simple and Exponential Moving Averages."""

    def test_sma_50d_warmup_and_calculation(self) -> None:
        num_days = 60
        base_date = date(2023, 1, 1)
        base_dt = datetime(2023, 1, 1, 21, 0, tzinfo=UTC)

        prices = pl.DataFrame(
            {
                "sec_id": ["SEC_1"] * num_days,
                "trade_date": [base_date + timedelta(days=i) for i in range(num_days)],
                "close": [float(i + 1) for i in range(num_days)],  # 1.0, 2.0, ... 60.0
                "known_from": [base_dt + timedelta(days=i) for i in range(num_days)],
            }
        )
        ctx = FeatureContext(prices=prices)

        result = compute_sma_50d(ctx)
        assert result.columns == ["sec_id", "trade_date", "known_from", "sma_50d"]

        # Exactly the first 49 values are null
        assert result["sma_50d"][:49].null_count() == 49

        # Value at index 49 (first 50 numbers 1..50) -> Mean = (1 + 50) / 2 = 25.5
        assert result["sma_50d"][49] == pytest.approx(25.5)
        # Value at index 50 (numbers 2..51) -> Mean = 26.5
        assert result["sma_50d"][50] == pytest.approx(26.5)

    def test_ema_50d_warmup_and_alpha_recursion(self) -> None:
        num_days = 60
        base_date = date(2023, 1, 1)
        base_dt = datetime(2023, 1, 1, 21, 0, tzinfo=UTC)

        prices = pl.DataFrame(
            {
                "sec_id": ["SEC_1"] * num_days,
                "trade_date": [base_date + timedelta(days=i) for i in range(num_days)],
                "close": [100.0] * num_days,
                "known_from": [base_dt + timedelta(days=i) for i in range(num_days)],
            }
        )
        ctx = FeatureContext(prices=prices)

        result = compute_ema_50d(ctx)
        assert result.columns == ["sec_id", "trade_date", "known_from", "ema_50d"]

        # Exactly the first 49 values are null
        assert result["ema_50d"][:49].null_count() == 49
        # Flat series -> EMA equals price (100.0)
        for idx in range(49, num_days):
            assert result["ema_50d"][idx] == pytest.approx(100.0)


# =============================================================================
# 5. Multi-Security Window Isolation
# =============================================================================


class TestMultiSecurityIsolation:
    """Assert technical indicators do not cross-contaminate across security boundaries."""

    def test_multi_security_momentum_isolation(self) -> None:
        num_days = 25
        base_date = date(2023, 1, 1)
        base_dt = datetime(2023, 1, 1, 21, 0, tzinfo=UTC)

        # Security A: 100 -> 120 (Momentum = +20%)
        # Security B: 200 -> 100 (Momentum = -50%)
        rows = []
        for i in range(num_days):
            rows.append(
                {
                    "sec_id": "SEC_A",
                    "trade_date": base_date + timedelta(days=i),
                    "close": 100.0 + (i if i <= 20 else 20.0),
                    "known_from": base_dt + timedelta(days=i),
                }
            )
            rows.append(
                {
                    "sec_id": "SEC_B",
                    "trade_date": base_date + timedelta(days=i),
                    "close": 200.0 - (5.0 * i if i <= 20 else 100.0),
                    "known_from": base_dt + timedelta(days=i),
                }
            )

        prices = pl.DataFrame(rows)
        ctx = FeatureContext(prices=prices)

        result = compute_momentum_20d(ctx)

        # Filter for day 20 per security
        res_a_20 = result.filter(
            (pl.col("sec_id") == "SEC_A") & (pl.col("trade_date") == base_date + timedelta(days=20))
        )
        res_b_20 = result.filter(
            (pl.col("sec_id") == "SEC_B") & (pl.col("trade_date") == base_date + timedelta(days=20))
        )

        assert res_a_20["momentum_20d"][0] == pytest.approx(0.20)
        assert res_b_20["momentum_20d"][0] == pytest.approx(-0.50)
