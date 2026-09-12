"""Unit tests for bitemporal mathematical interval logic and bound derivation."""

import zoneinfo
from datetime import date, datetime

import polars as pl
import pytest
from pydantic import ValidationError

from ledger.core.bitemporal import (
    TransactionInterval,
    ValidInterval,
    build_bitemporal_view_sql,
    derive_known_to_polars,
    derive_valid_to_polars,
    filter_point_in_time_polars,
)

UTC = zoneinfo.ZoneInfo("UTC")


class TestValidInterval:
    """Tests for ValidInterval model and half-open interval math [valid_from, valid_to)."""

    def test_open_interval_creation(self) -> None:
        start = date(2022, 1, 1)
        interval = ValidInterval(valid_from=start)
        assert interval.valid_from == start
        assert interval.valid_to is None
        assert interval.contains(date(2022, 1, 1))
        assert interval.contains(date(2025, 1, 1))
        assert not interval.contains(date(2021, 12, 31))

    def test_closed_interval_containment(self) -> None:
        start = datetime(2022, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2022, 6, 1, 0, 0, tzinfo=UTC)
        interval = ValidInterval(valid_from=start, valid_to=end)

        assert interval.contains(start)
        assert interval.contains(datetime(2022, 3, 1, 12, 0, tzinfo=UTC))
        # Half-open: upper bound is exclusive
        assert not interval.contains(end)
        assert not interval.contains(datetime(2022, 6, 1, 0, 1, tzinfo=UTC))
        assert not interval.contains(datetime(2021, 12, 31, 23, 59, tzinfo=UTC))

    def test_invalid_bounds_raise(self) -> None:
        start = date(2023, 1, 1)
        end = date(2022, 1, 1)
        with pytest.raises(ValidationError):
            ValidInterval(valid_from=start, valid_to=end)

    def test_equal_bounds_raise(self) -> None:
        point = date(2023, 1, 1)
        with pytest.raises(ValidationError):
            ValidInterval(valid_from=point, valid_to=point)

    def test_interval_overlaps(self) -> None:
        i1 = ValidInterval(valid_from=date(2022, 1, 1), valid_to=date(2022, 6, 1))
        i2 = ValidInterval(valid_from=date(2022, 5, 1), valid_to=date(2022, 12, 1))
        i3 = ValidInterval(valid_from=date(2022, 6, 1), valid_to=date(2022, 10, 1))
        i4 = ValidInterval(valid_from=date(2022, 7, 1))

        assert i1.overlaps(i2)
        assert i2.overlaps(i1)
        # i1 and i3 touch at 2022-06-01 but don't overlap since [a, b) and [b, c)
        assert not i1.overlaps(i3)
        assert not i1.overlaps(i4)
        assert i2.overlaps(i4)


class TestTransactionInterval:
    """Tests for TransactionInterval model and knowledge time [known_from, known_to)."""

    def test_active_knowledge_interval(self) -> None:
        t_known = datetime(2023, 5, 10, 16, 0, tzinfo=UTC)
        interval = TransactionInterval(known_from=t_known)

        assert interval.is_known_at(datetime(2023, 5, 10, 16, 0, tzinfo=UTC))
        assert interval.is_known_at(datetime(2023, 12, 31, 0, 0, tzinfo=UTC))
        assert not interval.is_known_at(datetime(2023, 5, 10, 15, 59, tzinfo=UTC))

    def test_superseded_knowledge_interval(self) -> None:
        t_original = datetime(2023, 5, 10, 16, 0, tzinfo=UTC)
        t_restated = datetime(2023, 11, 8, 17, 30, tzinfo=UTC)

        interval = TransactionInterval(known_from=t_original, known_to=t_restated)

        # Before filing -> not known
        assert not interval.is_known_at(datetime(2023, 5, 1, 0, 0, tzinfo=UTC))
        # Between original and restatement -> active knowledge
        assert interval.is_known_at(datetime(2023, 8, 1, 0, 0, tzinfo=UTC))
        # At and after restatement -> superseded, no longer active
        assert not interval.is_known_at(t_restated)
        assert not interval.is_known_at(datetime(2023, 12, 1, 0, 0, tzinfo=UTC))

    def test_invalid_transaction_bounds_raise(self) -> None:
        t1 = datetime(2023, 5, 10, 16, 0, tzinfo=UTC)
        t0 = datetime(2023, 5, 9, 16, 0, tzinfo=UTC)
        with pytest.raises(ValidationError):
            TransactionInterval(known_from=t1, known_to=t0)


class TestVectorizedDerivationHelpers:
    """Tests for Polars and SQL windowed derivation of upper bounds."""

    def test_derive_known_to_polars_restatement_sequence(self) -> None:
        raw_data = pl.DataFrame(
            {
                "sec_id": ["SEC001", "SEC001", "SEC002"],
                "metric_name": ["eps_diluted", "eps_diluted", "eps_diluted"],
                "metric_value": [1.00, 0.70, 2.50],
                "known_from": [
                    datetime(2023, 5, 10, 16, 0),
                    datetime(2023, 11, 8, 17, 30),
                    datetime(2023, 5, 10, 16, 0),
                ],
                "ingestion_seq": [1, 2, 3],
            }
        )

        derived = derive_known_to_polars(
            df=raw_data,
            partition_by=["sec_id", "metric_name"],
        )

        # Record 1 should have known_to set to record 2's known_from
        sec1_v1 = derived.filter((pl.col("sec_id") == "SEC001") & (pl.col("ingestion_seq") == 1))
        assert sec1_v1["known_to"][0] == datetime(2023, 11, 8, 17, 30)

        # Record 2 should have known_to as null (current active version)
        sec1_v2 = derived.filter((pl.col("sec_id") == "SEC001") & (pl.col("ingestion_seq") == 2))
        assert sec1_v2["known_to"][0] is None

        # SEC002 has only one version -> known_to is null
        sec2 = derived.filter(pl.col("sec_id") == "SEC002")
        assert sec2["known_to"][0] is None

    def test_derive_valid_to_polars_ticker_rename(self) -> None:
        raw_aliases = pl.DataFrame(
            {
                "sec_id": ["SEC_META", "SEC_META"],
                "ticker": ["FB", "META"],
                "valid_from": [
                    datetime(2012, 5, 18, 0, 0),
                    datetime(2022, 6, 9, 0, 0),
                ],
                "ingestion_seq": [1, 2],
            }
        )

        derived = derive_valid_to_polars(
            df=raw_aliases,
            partition_by="sec_id",
        )

        fb_row = derived.filter(pl.col("ticker") == "FB")
        assert fb_row["valid_to"][0] == datetime(2022, 6, 9, 0, 0)

        meta_row = derived.filter(pl.col("ticker") == "META")
        assert meta_row["valid_to"][0] is None

    def test_filter_point_in_time_polars(self) -> None:
        df = pl.DataFrame(
            {
                "sec_id": ["SEC001", "SEC001"],
                "metric_value": [1.00, 0.70],
                "known_from": [
                    datetime(2023, 5, 10, 16, 0),
                    datetime(2023, 11, 8, 17, 30),
                ],
                "known_to": [
                    datetime(2023, 11, 8, 17, 30),
                    None,
                ],
            }
        )

        # As of July 2023 (between v1 and v2)
        as_of_july = datetime(2023, 7, 1, 0, 0)
        pit_july = filter_point_in_time_polars(df, as_of=as_of_july)
        assert len(pit_july) == 1
        assert pit_july["metric_value"][0] == 1.00

        # As of December 2023 (after restatement)
        as_of_dec = datetime(2023, 12, 1, 0, 0)
        pit_dec = filter_point_in_time_polars(df, as_of=as_of_dec)
        assert len(pit_dec) == 1
        assert pit_dec["metric_value"][0] == 0.70

        # As of April 2023 (before initial filing)
        as_of_apr = datetime(2023, 4, 1, 0, 0)
        pit_apr = filter_point_in_time_polars(df, as_of=as_of_apr)
        assert len(pit_apr) == 0

    def test_build_bitemporal_view_sql(self) -> None:
        sql = build_bitemporal_view_sql(
            source_table="fact_fundamentals_raw",
            partition_by=["sec_id", "metric_name"],
            known_from_col="known_from",
            seq_col="ingestion_seq",
            known_to_col="known_to",
        )
        assert "LEAD(known_from) OVER" in sql
        assert "PARTITION BY sec_id, metric_name" in sql
        assert "ORDER BY known_from, ingestion_seq" in sql
        assert "FROM fact_fundamentals_raw" in sql
