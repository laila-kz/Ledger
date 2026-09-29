"""Tests for the raw price-basis invariant, bitemporal stamping, and replay safety.

Three defects motivated this file:

1. yfinance returns a price series split-adjusted over its *entire* history, so
   a bar dated before a later split arrived already pre-scaled by that split.
   Stored in a table named "raw", that is look-ahead: the 2022-08-25 TSLA 3:1
   split was visible in a 2020-08-28 price. Ingestion must undo the adjustment so
   a stored level depends only on events at or before its own date.

2. ``known_from`` was stamped a full session early. Ingestion anchored the
   actionable timestamp at naive midnight, which ``_to_ny_datetime`` reads as
   UTC; that resolves to the previous day in New York, so the calendar treated
   every bar as falling on a non-session and walked back to the previous close.
   The 2020-08-31 AAPL bar became visible on 2020-08-28 -- a session before it
   printed -- which also misaligned the ex-date pairing in the simulator and
   turned a true +3.4% day into +315.9%.

3. Storage is append-only, so re-running ingestion appended a second copy of
   every row. Seeding eight times produced 48,288 rows for 6,036 distinct
   business keys, which broke the ``IdempotentReplay`` invariant the bitemporal
   model is verified against.
"""

from datetime import date, datetime, time, timezone
from pathlib import Path

import pandas as pd
import polars as pl
import pytest

from ledger.core.calendars import get_actionable_timestamp
from ledger.ingestion.corporate_actions import parse_splits_series
from ledger.ingestion.market_data import _undo_full_history_split_adjustment
from ledger.storage.partitions import (
    filter_already_persisted,
    write_partitioned_corporate_actions,
    write_partitioned_market_ohlcv,
)


def _ohlcv(sec_id: str, rows: list[tuple[date, float, int]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "sec_id": [sec_id] * len(rows),
            "trade_date": [r[0] for r in rows],
            "open": [r[1] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[1] for r in rows],
            "close": [r[1] for r in rows],
            "volume": [r[2] for r in rows],
            "known_from": [
                datetime.combine(r[0], datetime.min.time(), tzinfo=timezone.utc) for r in rows
            ],
            "ingestion_seq": [1] * len(rows),
        }
    )


class TestRawPriceBasis:
    """A stored raw price must not encode a split that had not yet occurred."""

    def test_undoes_split_postdating_the_bar(self) -> None:
        """A bar before the split is scaled back up by that split's ratio."""
        df = _ohlcv("SEC_X_001", [(date(2020, 8, 28), 147.56, 1000)])
        splits = pl.DataFrame(
            {
                "sec_id": ["SEC_X_001", "SEC_X_001"],
                "ex_date": [date(2020, 8, 31), date(2022, 8, 25)],
                "split_ratio": [5.0, 3.0],
            }
        )

        out = _undo_full_history_split_adjustment(df, splits)

        # 147.56 * 5 * 3 = the true as-traded print.
        assert out["close"][0] == pytest.approx(147.56 * 15.0)

    def test_leaves_bar_on_ex_date_unscaled(self) -> None:
        """Ex-date prints are already post-split, so the ratio must not apply.

        The comparison is strict: ``ex_date > trade_date``.
        """
        df = _ohlcv("SEC_X_001", [(date(2020, 8, 31), 498.32, 1000)])
        splits = pl.DataFrame(
            {
                "sec_id": ["SEC_X_001"],
                "ex_date": [date(2020, 8, 31)],
                "split_ratio": [5.0],
            }
        )

        out = _undo_full_history_split_adjustment(df, splits)

        assert out["close"][0] == pytest.approx(498.32)

    def test_earlier_splits_do_not_rescale_current_bars(self) -> None:
        """A split that already happened needs no reversal for later bars."""
        df = _ohlcv("SEC_X_001", [(date(2022, 9, 1), 250.0, 1000)])
        splits = pl.DataFrame(
            {
                "sec_id": ["SEC_X_001"],
                "ex_date": [date(2020, 8, 31)],
                "split_ratio": [5.0],
            }
        )

        out = _undo_full_history_split_adjustment(df, splits)

        assert out["close"][0] == pytest.approx(250.0)

    def test_applies_whole_ohlc_but_not_volume(self) -> None:
        """Prices rescale; share volume is already as-traded and must not."""
        df = _ohlcv("SEC_X_001", [(date(2020, 8, 28), 100.0, 12345)])
        splits = pl.DataFrame(
            {
                "sec_id": ["SEC_X_001"],
                "ex_date": [date(2020, 8, 31)],
                "split_ratio": [4.0],
            }
        )

        out = _undo_full_history_split_adjustment(df, splits)

        assert out["open"][0] == pytest.approx(400.0)
        assert out["high"][0] == pytest.approx(400.0)
        assert out["low"][0] == pytest.approx(400.0)
        assert out["close"][0] == pytest.approx(400.0)
        assert out["volume"][0] == 12345

    def test_no_splits_is_a_passthrough(self) -> None:
        df = _ohlcv("SEC_X_001", [(date(2020, 8, 28), 100.0, 1)])

        assert _undo_full_history_split_adjustment(df, None).equals(df)
        assert _undo_full_history_split_adjustment(df, pl.DataFrame()).equals(df)

    def test_splits_for_other_securities_are_ignored(self) -> None:
        """A factor must never bleed across securities."""
        df = _ohlcv("SEC_A_001", [(date(2020, 8, 28), 100.0, 1)])
        splits = pl.DataFrame(
            {
                "sec_id": ["SEC_B_001"],
                "ex_date": [date(2020, 8, 31)],
                "split_ratio": [20.0],
            }
        )

        out = _undo_full_history_split_adjustment(df, splits)

        assert out["close"][0] == pytest.approx(100.0)

    def test_pit_caf_rebuilt_from_as_traded_is_step_free_except_at_ex_dates(self) -> None:
        """Undoing then re-adjusting reproduces the as-traded series exactly.

        The pipeline contract is: ingestion stores as-traded levels, and the CAF
        feature re-derives an adjusted view at query time from the split table.
        So the round trip that must hold is

            undo(provider_series) == true as-traded print
            adj_close(t) = as_traded(t) * product(1 / split_ratio), splits known at t

        This test builds the true as-traded path first and derives the provider
        series from it, so any error in the undo step shows up as a mismatch
        rather than being masked by a hand-picked fixture.
        """
        ratios = {date(2020, 8, 31): 5.0, date(2022, 8, 25): 3.0}

        # The real as-traded path, including the genuine economic level shifts
        # that splits cause. 2020-08-31 is the 5:1 ex-date (~2213 -> ~498).
        as_traded_truth = {
            date(2020, 8, 27): 2246.25,
            date(2020, 8, 28): 2213.40,
            date(2020, 8, 31): 498.32,
            date(2022, 8, 24): 3715.20,
            date(2022, 8, 25): 1250.00,
        }

        # The provider folds in every split that post-dates the bar.
        provider: dict[date, float] = {}
        for trade_date, as_traded_px in as_traded_truth.items():
            factor = 1.0
            for ex_date, ratio in ratios.items():
                if ex_date > trade_date:
                    factor *= ratio
            provider[trade_date] = as_traded_px / factor

        # A pre-split bar really is pre-scaled by the provider, which is the
        # look-ahead being removed.
        assert provider[date(2020, 8, 28)] == pytest.approx(147.56)
        assert as_traded_truth[date(2020, 8, 28)] / provider[date(2020, 8, 28)] == (
            pytest.approx(15.0)
        )

        df = _ohlcv("SEC_X_001", [(d, px, 1) for d, px in provider.items()])
        splits = pl.DataFrame(
            {
                "sec_id": ["SEC_X_001"] * len(ratios),
                "ex_date": list(ratios),
                "split_ratio": list(ratios.values()),
            }
        )

        as_traded = _undo_full_history_split_adjustment(df, splits)

        # Round trip 1: the store holds the true as-traded print again.
        for row in as_traded.iter_rows(named=True):
            assert float(row["close"]) == pytest.approx(as_traded_truth[row["trade_date"]])

        # Round trip 2: the CAF feature re-adjusts at query time. Note CAF is a
        # product of 1/split_ratio over splits already known at t.
        adjusted_pit: dict[date, float] = {}
        for row in as_traded.iter_rows(named=True):
            trade_date = row["trade_date"]
            caf = 1.0
            for ex_date, ratio in ratios.items():
                if ex_date <= trade_date:
                    caf *= 1.0 / ratio
            adjusted_pit[trade_date] = float(row["close"]) * caf

        # Before the first split CAF is 1.0, so the level is untouched.
        assert adjusted_pit[date(2020, 8, 28)] == pytest.approx(2213.40)
        # The 2020 ex-date bar has absorbed the 5:1 but not the later 3:1.
        assert adjusted_pit[date(2020, 8, 31)] == pytest.approx(498.32 * 0.2)
        # By 2022 both splits are known.
        assert adjusted_pit[date(2022, 8, 25)] == pytest.approx(1250.00 / 15.0)

        # The PIT series carries exactly one deliberate step per ex-date.
        # Multiplying that step out by the known ratio -- what the simulator
        # does -- must recover the true as-traded return. Everywhere else the
        # adjusted return must already equal it. This is the no-leakage
        # correction the backtest depends on.
        dates = sorted(adjusted_pit)
        for prev, cur in zip(dates, dates[1:], strict=False):
            adj_return = adjusted_pit[cur] / adjusted_pit[prev]
            true_return = as_traded_truth[cur] / as_traded_truth[prev]
            corrected = adj_return * ratios.get(cur, 1.0)
            assert corrected == pytest.approx(true_return)


class TestIdempotentReingestion:
    """Re-ingesting an unchanged range must not append duplicate rows."""

    def test_filter_drops_identical_existing_rows(self, tmp_path: Path) -> None:
        df = _ohlcv("SEC_X_001", [(date(2020, 1, 2), 100.0, 1)])
        write_partitioned_market_ohlcv(df, base_dir=tmp_path, ingestion_seq=1)

        replay = df.with_columns(pl.lit(2).alias("ingestion_seq"))
        remaining = filter_already_persisted(
            replay, tmp_path, "market_ohlcv", ["sec_id", "trade_date", "known_from"]
        )

        assert remaining.is_empty()

    def test_changed_payload_at_same_key_is_retained(self, tmp_path: Path) -> None:
        """A correction is not a replay and must survive."""
        df = _ohlcv("SEC_X_001", [(date(2020, 1, 2), 100.0, 1)])
        write_partitioned_market_ohlcv(df, base_dir=tmp_path, ingestion_seq=1)

        corrected = _ohlcv("SEC_X_001", [(date(2020, 1, 2), 101.0, 1)])
        remaining = filter_already_persisted(
            corrected, tmp_path, "market_ohlcv", ["sec_id", "trade_date", "known_from"]
        )

        assert len(remaining) == 1

    def test_repeated_writes_do_not_grow_the_table(self, tmp_path: Path) -> None:
        df = _ohlcv("SEC_X_001", [(date(2020, 1, 2), 100.0, 1), (date(2020, 1, 3), 101.0, 2)])

        first = write_partitioned_market_ohlcv(df, base_dir=tmp_path, ingestion_seq=1)
        assert len(first) == 1

        for seq in (2, 3):
            assert (
                write_partitioned_market_ohlcv(
                    df.with_columns(pl.lit(seq).alias("ingestion_seq")),
                    base_dir=tmp_path,
                    ingestion_seq=seq,
                )
                == []
            )

        stored = pl.concat(
            [pl.read_parquet(p) for p in sorted((tmp_path / "market_ohlcv").glob("**/*.parquet"))]
        )
        assert len(stored) == 2

    def test_corporate_actions_replay_is_a_noop(self, tmp_path: Path) -> None:
        """Duplicate split events were the cause of compounded split factors."""

        def _actions(seq: int) -> pl.DataFrame:
            return pl.DataFrame(
                {
                    "sec_id": ["SEC_X_001"],
                    "action_type": ["SPLIT"],
                    "ex_date": [date(2020, 8, 31)],
                    "known_from": [datetime(2020, 8, 31, 20, 15, tzinfo=timezone.utc)],
                    "value": [1.0],
                    "currency": [None],
                    "split_ratio": [4.0],
                    "cash_amount": [None],
                    "ingestion_seq": [seq],
                },
                schema={
                    "sec_id": pl.String,
                    "action_type": pl.String,
                    "ex_date": pl.Date,
                    "known_from": pl.Datetime("us", "UTC"),
                    "value": pl.Float64,
                    "currency": pl.String,
                    "split_ratio": pl.Float64,
                    "cash_amount": pl.Float64,
                    "ingestion_seq": pl.Int64,
                },
            )

        assert len(write_partitioned_corporate_actions(_actions(1), tmp_path, 1)) == 1
        for seq in (2, 3):
            assert write_partitioned_corporate_actions(_actions(seq), tmp_path, seq) == []

        root = tmp_path / "corporate_actions"
        stored = pl.concat([pl.read_parquet(p) for p in sorted(root.glob("**/*.parquet"))])
        assert len(stored) == 1
        assert stored["split_ratio"][0] == pytest.approx(4.0)


class TestKnownFromStamping:
    """A fact must not become actionable before the session it describes."""

    def test_midnight_anchor_would_collapse_to_the_previous_session(self) -> None:
        """Documents the bug: naive midnight is read as UTC, i.e. the day before."""
        trade_date = date(2020, 8, 31)
        naive_midnight = datetime.combine(trade_date, time.min)
        # Read as UTC, midnight is 20:00 the previous day in New York.
        from ledger.core.calendars import default_nyse_calendar as cal

        assert cal._to_ny_datetime(naive_midnight).date() == date(2020, 8, 30)
        assert not cal.is_session(date(2020, 8, 30))
        # So the actionable stamp lands on the prior session, a day early.
        wrong = get_actionable_timestamp(naive_midnight, is_market_data=True)
        assert wrong.astimezone(timezone.utc).date() == date(2020, 8, 28)

    def test_midday_anchor_preserves_the_session_date(self) -> None:
        trade_date = date(2020, 8, 31)
        midday = datetime.combine(trade_date, time(hour=12))
        actionable = get_actionable_timestamp(midday, is_market_data=True)

        assert actionable.astimezone(timezone.utc).date() == trade_date
        # 16:15 America/New_York == 20:15 UTC on a summer session.
        assert actionable.hour == 16
        assert actionable.minute == 15

    def test_split_records_carry_sec_id(self) -> None:
        """A split with no sec_id is detached from its security and unusable."""
        series = pd.Series([4.0], index=pd.to_datetime(["2020-08-31"]))
        records = parse_splits_series(series, sec_id="SEC_AAPL_001", ingestion_seq=1)

        assert len(records) == 1
        assert records[0]["sec_id"] == "SEC_AAPL_001"

    def test_split_known_from_is_its_own_ex_date_session(self) -> None:
        series = pd.Series([4.0], index=pd.to_datetime(["2020-08-31"]))
        records = parse_splits_series(series, sec_id="SEC_AAPL_001", ingestion_seq=1)

        known_from = records[0]["known_from"]
        assert isinstance(known_from, datetime), (
            f"known_from must be a datetime, got {type(known_from).__name__}"
        )
        assert known_from.date() == date(2020, 8, 31)
        # The bar for that same session is actionable no earlier than the split,
        # so a simulator pairing them cannot see the split early.
        assert known_from == datetime(2020, 8, 31, 20, 15, tzinfo=timezone.utc)
