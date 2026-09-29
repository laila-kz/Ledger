"""Unit tests for the Dynamic Cumulative Adjustment Factor (CAF) engine.

These tests encode the canonical AAPL 4-for-1 split scenario (ex_date 2020-08-31,
known_from 2020-08-31 20:15 UTC) as the ground truth against which the engine is
validated. Every assertion in this file has a direct mapping to a cell in the
worked example table from the Week 2 guide and ADR 005.

Split event under test:
    Security : AAPL  (sec_id = "SEC_AAPL_001")
    Split     : 4:1 on 2020-08-31
    known_from: 2020-08-31 20:15:00 UTC (conservative post-close recording)

CAF truth table:
    T_obs                   | Splits in scope | CAF  | adj_close (raw=500)
    2020-07-28 20:15 UTC    | none            | 1.0  | 500.0
    2020-08-15 20:15 UTC    | none            | 1.0  | 500.0
    2020-08-31 15:00 UTC    | none            | 1.0  | 500.0  <- known_from guard
    2020-08-31 21:00 UTC    | {split}         | 0.25 | 125.0
    2020-09-15 20:15 UTC    | {split}         | 0.25 | 125.0
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import polars as pl
import pytest

from ledger.features.caf import (
    adjusted_close_as_of,
    compute_caf_matrix,
    compute_caf_scalar,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

SEC_ID = "SEC_AAPL_001"
PRICE_DATE = date(2020, 7, 28)
RAW_CLOSE = 500.0

# The AAPL split: 4:1 on 2020-08-31, recorded post-market at 20:15 UTC
SPLIT_EX_DATE = date(2020, 8, 31)
SPLIT_KNOWN_FROM = datetime(2020, 8, 31, 20, 15, 0, tzinfo=timezone.utc)
SPLIT_RATIO = 4.0


@pytest.fixture()
def splits_df() -> pl.DataFrame:
    """One AAPL 4:1 split corporate action."""
    return pl.DataFrame(
        {
            "sec_id": [SEC_ID],
            "ex_date": [SPLIT_EX_DATE],
            "split_ratio": [SPLIT_RATIO],
            "known_from": [SPLIT_KNOWN_FROM],
        },
        schema={
            "sec_id": pl.Utf8,
            "ex_date": pl.Date,
            "split_ratio": pl.Float64,
            "known_from": pl.Datetime("us", "UTC"),
        },
    )


@pytest.fixture()
def prices_df() -> pl.DataFrame:
    """Raw OHLCV for AAPL with several dates spanning the split."""
    dates = [
        date(2020, 7, 28),
        date(2020, 8, 14),
        date(2020, 8, 28),
        date(2020, 8, 31),
        date(2020, 9, 1),
        date(2020, 9, 15),
    ]
    closes = [500.0, 490.0, 499.0, 129.0, 134.0, 115.0]  # raw (split-unadjusted)
    return pl.DataFrame(
        {
            "sec_id": [SEC_ID] * len(dates),
            "trade_date": dates,
            "close": closes,
        },
        schema={
            "sec_id": pl.Utf8,
            "trade_date": pl.Date,
            "close": pl.Float64,
        },
    )


# ---------------------------------------------------------------------------
# Test suite A: Scalar reference implementation
# ---------------------------------------------------------------------------


class TestComputeCAFScalar:
    """Tests for the scalar (reference) CAF function."""

    def test_before_split_early_observation_caf_is_one(self, splits_df: pl.DataFrame) -> None:
        """Observation well before ex_date -> no splits in scope -> CAF=1.0."""
        caf = compute_caf_scalar(
            price_date=PRICE_DATE,
            observation_timestamp=datetime(2020, 7, 28, 20, 15, 0, tzinfo=timezone.utc),
            splits_df=splits_df,
            sec_id=SEC_ID,
        )
        assert caf == 1.0, f"Expected CAF=1.0 before split, got {caf}"

    def test_mid_august_observation_caf_is_one(self, splits_df: pl.DataFrame) -> None:
        """Observation in August before ex_date -> no splits in scope -> CAF=1.0."""
        caf = compute_caf_scalar(
            price_date=PRICE_DATE,
            observation_timestamp=datetime(2020, 8, 15, 20, 15, 0, tzinfo=timezone.utc),
            splits_df=splits_df,
            sec_id=SEC_ID,
        )
        assert caf == 1.0, f"Expected CAF=1.0 on 2020-08-15 observation, got {caf}"

    def test_ex_date_before_known_from_caf_is_one(self, splits_df: pl.DataFrame) -> None:
        """Observation on ex_date but BEFORE known_from (15:00 vs 20:15 UTC) -> CAF=1.0.

        This is the critical known_from guard test. The split is effective today
        but we haven't learned about it yet (market is still open / data not yet
        consolidated). The CAF must stay at 1.0 to avoid lookahead.
        """
        obs_ts = datetime(2020, 8, 31, 15, 0, 0, tzinfo=timezone.utc)
        assert obs_ts < SPLIT_KNOWN_FROM, "Precondition: obs_ts must be before known_from"

        caf = compute_caf_scalar(
            price_date=PRICE_DATE,
            observation_timestamp=obs_ts,
            splits_df=splits_df,
            sec_id=SEC_ID,
        )
        assert caf == 1.0, (
            f"known_from guard failed: expected CAF=1.0 at 15:00 UTC on ex_date, got {caf}"
        )

    def test_ex_date_after_known_from_caf_is_quarter(self, splits_df: pl.DataFrame) -> None:
        """Observation on ex_date AFTER known_from (21:00 vs 20:15 UTC) -> CAF=0.25."""
        obs_ts = datetime(2020, 8, 31, 21, 0, 0, tzinfo=timezone.utc)
        assert obs_ts > SPLIT_KNOWN_FROM, "Precondition: obs_ts must be after known_from"

        caf = compute_caf_scalar(
            price_date=PRICE_DATE,
            observation_timestamp=obs_ts,
            splits_df=splits_df,
            sec_id=SEC_ID,
        )
        assert caf == pytest.approx(0.25), (
            f"Expected CAF=0.25 after known_from on ex_date, got {caf}"
        )

    def test_post_split_observation_caf_is_quarter(self, splits_df: pl.DataFrame) -> None:
        """Observation well after ex_date and known_from -> CAF=0.25."""
        caf = compute_caf_scalar(
            price_date=PRICE_DATE,
            observation_timestamp=datetime(2020, 9, 15, 20, 15, 0, tzinfo=timezone.utc),
            splits_df=splits_df,
            sec_id=SEC_ID,
        )
        assert caf == pytest.approx(0.25), f"Expected CAF=0.25 post-split, got {caf}"

    def test_no_splits_returns_one(self) -> None:
        """Empty splits DataFrame always returns CAF=1.0."""
        empty_splits = pl.DataFrame(
            schema={
                "sec_id": pl.Utf8,
                "ex_date": pl.Date,
                "split_ratio": pl.Float64,
                "known_from": pl.Datetime("us", "UTC"),
            }
        )
        caf = compute_caf_scalar(
            price_date=PRICE_DATE,
            observation_timestamp=datetime(2020, 9, 15, 20, 15, 0, tzinfo=timezone.utc),
            splits_df=empty_splits,
            sec_id=SEC_ID,
        )
        assert caf == 1.0

    def test_different_security_split_not_applied(self, splits_df: pl.DataFrame) -> None:
        """A split for a different sec_id must not affect our security's CAF."""
        caf = compute_caf_scalar(
            price_date=PRICE_DATE,
            observation_timestamp=datetime(2020, 9, 15, 20, 15, 0, tzinfo=timezone.utc),
            splits_df=splits_df,
            sec_id="SEC_MSFT_001",  # Different security
        )
        assert caf == 1.0

    def test_price_date_same_as_ex_date_excluded(self, splits_df: pl.DataFrame) -> None:
        """A split on the same date as the price date is excluded (predicate: t < ex_date).

        We are computing the adjusted close OF the split day's price, which is already
        recorded post-split. The CAF should not double-count it.
        """
        caf = compute_caf_scalar(
            price_date=SPLIT_EX_DATE,  # price_date == ex_date -> strict < -> excluded
            observation_timestamp=datetime(2020, 9, 15, 20, 15, 0, tzinfo=timezone.utc),
            splits_df=splits_df,
            sec_id=SEC_ID,
        )
        assert caf == 1.0, (
            f"Split on same date as price should be excluded (t < ex_date is strict), got {caf}"
        )

    def test_compound_splits_product(self) -> None:
        """Two historical splits compound correctly: CAF = (1/4.0) * (1/7.0) = 1/28.0."""
        two_splits = pl.DataFrame(
            {
                "sec_id": [SEC_ID, SEC_ID],
                "ex_date": [date(2020, 6, 1), date(2020, 8, 31)],
                "split_ratio": [7.0, 4.0],
                "known_from": [
                    datetime(2020, 6, 1, 20, 15, 0, tzinfo=timezone.utc),
                    datetime(2020, 8, 31, 20, 15, 0, tzinfo=timezone.utc),
                ],
            },
            schema={
                "sec_id": pl.Utf8,
                "ex_date": pl.Date,
                "split_ratio": pl.Float64,
                "known_from": pl.Datetime("us", "UTC"),
            },
        )
        caf = compute_caf_scalar(
            price_date=date(2020, 1, 15),
            observation_timestamp=datetime(2020, 9, 15, 20, 15, 0, tzinfo=timezone.utc),
            splits_df=two_splits,
            sec_id=SEC_ID,
        )
        assert caf == pytest.approx(1.0 / 28.0), f"Expected compound CAF={1.0 / 28.0}, got {caf}"


# ---------------------------------------------------------------------------
# Test suite B: Vectorized matrix engine
# ---------------------------------------------------------------------------


class TestComputeCAFMatrix:
    """Tests for the vectorized compute_caf_matrix engine."""

    def _make_obs_df(self, timestamps: list[datetime]) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "sec_id": [SEC_ID] * len(timestamps),
                "observation_timestamp": timestamps,
            },
            schema={
                "sec_id": pl.Utf8,
                "observation_timestamp": pl.Datetime("us", "UTC"),
            },
        )

    def test_matrix_pre_split_row_has_caf_one(
        self, prices_df: pl.DataFrame, splits_df: pl.DataFrame
    ) -> None:
        """All rows in the matrix for a pre-split observation should have CAF=1.0."""
        obs_df = self._make_obs_df([datetime(2020, 8, 15, 20, 15, 0, tzinfo=timezone.utc)])
        result = compute_caf_matrix(prices_df, splits_df, obs_df)
        pre_split_rows = result.filter(pl.col("trade_date") < SPLIT_EX_DATE)
        assert pre_split_rows["caf"].to_list() == pytest.approx([1.0] * len(pre_split_rows))

    def test_matrix_post_split_historical_rows_have_caf_quarter(
        self, prices_df: pl.DataFrame, splits_df: pl.DataFrame
    ) -> None:
        """Historical prices (trade_date < ex_date) viewed post-split have CAF=0.25."""
        obs_df = self._make_obs_df([datetime(2020, 9, 15, 20, 15, 0, tzinfo=timezone.utc)])
        result = compute_caf_matrix(prices_df, splits_df, obs_df)
        pre_ex_date_rows = result.filter(pl.col("trade_date") < SPLIT_EX_DATE)
        cafs = pre_ex_date_rows["caf"].to_list()
        assert all(c == pytest.approx(0.25) for c in cafs), (
            f"Expected all pre-ex-date rows to have CAF=0.25, got {cafs}"
        )

    def test_matrix_split_ex_date_row_has_caf_one(
        self, prices_df: pl.DataFrame, splits_df: pl.DataFrame
    ) -> None:
        """Price on the split ex_date itself should not be adjusted (t < ex_date is strict)."""
        obs_df = self._make_obs_df([datetime(2020, 9, 15, 20, 15, 0, tzinfo=timezone.utc)])
        result = compute_caf_matrix(prices_df, splits_df, obs_df)
        ex_date_row = result.filter(pl.col("trade_date") == SPLIT_EX_DATE)
        assert len(ex_date_row) == 1
        assert ex_date_row["caf"][0] == pytest.approx(1.0)

    def test_matrix_adj_close_equals_close_times_caf(
        self, prices_df: pl.DataFrame, splits_df: pl.DataFrame
    ) -> None:
        """adj_close = close * caf must hold for every row in the result."""
        obs_df = self._make_obs_df([datetime(2020, 9, 15, 20, 15, 0, tzinfo=timezone.utc)])
        result = compute_caf_matrix(prices_df, splits_df, obs_df)
        # Compute expected adj_close from close and caf columns
        expected = (result["close"] * result["caf"]).to_list()
        actual = result["adj_close"].to_list()
        assert actual == pytest.approx(expected)

    def test_matrix_known_from_guard_vectorized(
        self, prices_df: pl.DataFrame, splits_df: pl.DataFrame
    ) -> None:
        """known_from guard works correctly in the vectorized path.

        Observation at 15:00 UTC on ex_date (before known_from 20:15 UTC):
        historical rows must have CAF=1.0, not 0.25.
        """
        obs_ts_before = datetime(2020, 8, 31, 15, 0, 0, tzinfo=timezone.utc)
        obs_ts_after = datetime(2020, 8, 31, 21, 0, 0, tzinfo=timezone.utc)

        obs_df = self._make_obs_df([obs_ts_before, obs_ts_after])
        result = compute_caf_matrix(prices_df, splits_df, obs_df)

        # For price_date = 2020-07-28 (well before split):
        before_guard = result.filter(
            (pl.col("observation_timestamp") == obs_ts_before)
            & (pl.col("trade_date") == date(2020, 7, 28))
        )
        assert len(before_guard) == 1
        assert before_guard["caf"][0] == pytest.approx(1.0), (
            "known_from guard failed in vectorized path: pre-known_from should be CAF=1.0"
        )

        after_guard = result.filter(
            (pl.col("observation_timestamp") == obs_ts_after)
            & (pl.col("trade_date") == date(2020, 7, 28))
        )
        assert len(after_guard) == 1
        assert after_guard["caf"][0] == pytest.approx(0.25), (
            "Post known_from should be CAF=0.25 in vectorized path"
        )

    def test_matrix_missing_column_raises(
        self, prices_df: pl.DataFrame, splits_df: pl.DataFrame
    ) -> None:
        """Missing required column raises ValueError."""
        obs_df = self._make_obs_df([datetime(2020, 9, 15, 20, 15, 0, tzinfo=timezone.utc)])
        bad_prices = prices_df.drop("close")
        with pytest.raises(ValueError, match="missing required columns"):
            compute_caf_matrix(bad_prices, splits_df, obs_df)


# ---------------------------------------------------------------------------
# Test suite C: adjusted_close_as_of convenience wrapper
# ---------------------------------------------------------------------------


class TestAdjustedCloseAsOf:
    """Tests for the adjusted_close_as_of() convenience wrapper."""

    def test_returns_sorted_by_trade_date(
        self, prices_df: pl.DataFrame, splits_df: pl.DataFrame
    ) -> None:
        obs_ts = datetime(2020, 9, 15, 20, 15, 0, tzinfo=timezone.utc)
        result = adjusted_close_as_of(prices_df, splits_df, obs_ts, SEC_ID)
        dates = result["trade_date"].to_list()
        assert dates == sorted(dates), "Result should be sorted by trade_date"

    def test_pre_split_close_unchanged(
        self, prices_df: pl.DataFrame, splits_df: pl.DataFrame
    ) -> None:
        """Pre-split observation: adj_close must equal raw close (CAF=1.0)."""
        obs_ts = datetime(2020, 8, 15, 20, 15, 0, tzinfo=timezone.utc)
        result = adjusted_close_as_of(prices_df, splits_df, obs_ts, SEC_ID)
        assert result["adj_close"].to_list() == pytest.approx(result["close"].to_list())

    def test_post_split_pre_ex_date_prices_scaled_by_quarter(
        self, prices_df: pl.DataFrame, splits_df: pl.DataFrame
    ) -> None:
        """Post-split observation: prices before ex_date are scaled 0.25x (divided by 4)."""
        obs_ts = datetime(2020, 9, 15, 20, 15, 0, tzinfo=timezone.utc)
        result = adjusted_close_as_of(prices_df, splits_df, obs_ts, SEC_ID)
        pre_split = result.filter(pl.col("trade_date") < SPLIT_EX_DATE)
        for row in pre_split.iter_rows(named=True):
            assert row["adj_close"] == pytest.approx(row["close"] * 0.25), (
                f"Expected adj_close = close * 0.25 for {row['trade_date']}, "
                f"got close={row['close']}, adj_close={row['adj_close']}"
            )
