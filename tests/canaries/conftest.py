"""Shared deterministic data builders and leakage references for canary tests."""

from __future__ import annotations

from collections.abc import Iterable as _Iterable
from collections.abc import Sequence as _Sequence
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import time as _time
from datetime import timedelta as _timedelta
from datetime import timezone as _timezone
from typing import Any as _Any

import polars as pl
from polars.testing import assert_frame_equal as _assert_frame_equal
from polars.testing import assert_series_equal as _assert_series_equal

UTC = _timezone.utc


def _as_date(value: _date | _datetime | str) -> _date:
    if isinstance(value, _datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    return _date.fromisoformat(value)


def _as_datetime(value: _datetime | str, *, default_time: _time = _time(0)) -> _datetime:
    if isinstance(value, _datetime):
        parsed = value
    else:
        parsed = _datetime.fromisoformat(value.replace(" UTC", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _business_dates(start: _date, end: _date) -> list[_date]:
    dates: list[_date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            dates.append(current)
        current += _timedelta(days=1)
    return dates


def build_price_series(
    sec_id: str = "SEC_AAPL_001",
    start: _date | str = "2020-07-01",
    end: _date | str = "2020-09-30",
    base_price: float = 500.0,
    daily_step: float = 1.0,
    known_time: _time = _time(21, 0),
) -> pl.DataFrame:
    """Build deterministic weekday OHLCV bars with UTC knowledge timestamps."""
    dates = _business_dates(_as_date(start), _as_date(end))
    rows: list[dict[str, _Any]] = []
    for index, trade_date in enumerate(dates):
        close = base_price + (index * daily_step)
        known_from = _datetime.combine(trade_date, known_time, tzinfo=UTC)
        rows.append(
            {
                "sec_id": sec_id,
                "trade_date": trade_date,
                "open": close - 1.0,
                "high": close + 2.0,
                "low": close - 2.0,
                "close": close,
                "volume": 1_000_000 + index,
                "known_from": known_from,
                "ingestion_seq": index + 1,
            }
        )
    return pl.DataFrame(
        rows,
        schema={
            "sec_id": pl.Utf8,
            "trade_date": pl.Date,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
            "known_from": pl.Datetime("us", "UTC"),
            "ingestion_seq": pl.Int64,
        },
    )


def build_split(
    sec_id: str = "SEC_AAPL_001",
    ex_date: _date | str = "2020-08-31",
    ratio: float = 4.0,
    known_from: _datetime | str = "2020-08-31 20:15 UTC",
    announcement_date: _date | str = "2020-08-11",
) -> pl.DataFrame:
    """Build one deterministic split event in the corporate-action schema."""
    return pl.DataFrame(
        {
            "sec_id": [sec_id],
            "action_type": ["SPLIT"],
            "ex_date": [_as_date(ex_date)],
            "split_ratio": [ratio],
            "cash_amount": [None],
            "announcement_date": [_as_date(announcement_date)],
            "known_from": [_as_datetime(known_from)],
            "ingestion_seq": [1],
        },
        schema={
            "sec_id": pl.Utf8,
            "action_type": pl.Utf8,
            "ex_date": pl.Date,
            "split_ratio": pl.Float64,
            "cash_amount": pl.Float64,
            "announcement_date": pl.Date,
            "known_from": pl.Datetime("us", "UTC"),
            "ingestion_seq": pl.Int64,
        },
    )


def build_dividend(
    sec_id: str = "SEC_AAPL_001",
    ex_date: _date | str = "2020-08-31",
    amount: float = 1.0,
    known_from: _datetime | str = "2020-08-31 20:15 UTC",
) -> pl.DataFrame:
    """Build one deterministic cash-dividend event for future canaries."""
    return pl.DataFrame(
        {
            "sec_id": [sec_id],
            "action_type": ["CASH_DIVIDEND"],
            "ex_date": [_as_date(ex_date)],
            "split_ratio": [None],
            "cash_amount": [amount],
            "announcement_date": [_as_date(ex_date)],
            "known_from": [_as_datetime(known_from)],
            "ingestion_seq": [1],
        },
        schema={
            "sec_id": pl.Utf8,
            "action_type": pl.Utf8,
            "ex_date": pl.Date,
            "split_ratio": pl.Float64,
            "cash_amount": pl.Float64,
            "announcement_date": pl.Date,
            "known_from": pl.Datetime("us", "UTC"),
            "ingestion_seq": pl.Int64,
        },
    )


def build_fundamental(
    sec_id: str = "SEC_AAPL_001",
    period_end: _date | str = "2023-06-30",
    metric: str = "eps",
    value: float = 1.0,
    known_from: _datetime | str = "2023-08-01 17:30 UTC",
    filing_type: str = "10-Q",
    ingestion_seq: int = 1,
    is_restatement: bool = False,
) -> pl.DataFrame:
    """Build one filing version; append multiple results for a restatement scenario."""
    return pl.DataFrame(
        {
            "sec_id": [sec_id],
            "filing_type": [filing_type],
            "fiscal_period_end": [_as_date(period_end)],
            "metric_name": [metric],
            "metric_value": [value],
            "known_from": [_as_datetime(known_from)],
            "is_restatement": [is_restatement],
            "ingestion_seq": [ingestion_seq],
        },
        schema={
            "sec_id": pl.Utf8,
            "filing_type": pl.Utf8,
            "fiscal_period_end": pl.Date,
            "metric_name": pl.Utf8,
            "metric_value": pl.Float64,
            "known_from": pl.Datetime("us", "UTC"),
            "is_restatement": pl.Boolean,
            "ingestion_seq": pl.Int64,
        },
    )


def build_entity_map(
    sec_id: str = "SEC_META_001",
    ticker: str = "FB",
    valid_from: _datetime | str = "2012-05-18 00:00 UTC",
    ingestion_seq: int = 1,
) -> pl.DataFrame:
    """Build one append-only ticker alias record."""
    return pl.DataFrame(
        {
            "sec_id": [sec_id],
            "ticker": [ticker],
            "valid_from": [_as_datetime(valid_from)],
            "ingestion_seq": [ingestion_seq],
        },
        schema={
            "sec_id": pl.Utf8,
            "ticker": pl.Utf8,
            "valid_from": pl.Datetime("us", "UTC"),
            "ingestion_seq": pl.Int64,
        },
    )


def leaky_join_on_trade_date(
    features: pl.DataFrame,
    observations: pl.DataFrame,
) -> pl.DataFrame:
    """Deliberately leak future bars by joining on valid trade date only."""
    left = observations.with_columns(
        pl.col("observation_timestamp").dt.date().alias("_observation_date")
    )
    return left.join(
        features,
        left_on=["sec_id", "_observation_date"],
        right_on=["sec_id", "trade_date"],
        how="left",
    ).drop("_observation_date")


def leaky_static_adjusted_close(
    prices: pl.DataFrame,
    splits: pl.DataFrame,
) -> pl.DataFrame:
    """Deliberately apply every split retroactively, regardless of knowledge time."""
    split_factors = splits.group_by("sec_id").agg(pl.col("split_ratio").product().alias("caf"))
    return prices.join(split_factors, on="sec_id", how="left").with_columns(
        pl.col("caf").fill_null(1.0),
        (pl.col("close") * pl.col("caf")).alias("adj_close"),
    )


def leaky_same_day_filing(
    filings: pl.DataFrame,
    observations: pl.DataFrame,
) -> pl.DataFrame:
    """Deliberately treat a fiscal period end as its availability date."""
    left = observations.with_columns(
        pl.col("observation_timestamp").dt.date().alias("_observation_date")
    ).sort(["sec_id", "_observation_date"])
    right = filings.sort(["sec_id", "fiscal_period_end"])
    return left.join_asof(
        right,
        left_on="_observation_date",
        right_on="fiscal_period_end",
        by="sec_id",
        strategy="backward",
    ).drop("_observation_date")


def leaky_latest_fundamental(
    filings: pl.DataFrame,
    observations: pl.DataFrame,
) -> pl.DataFrame:
    """Deliberately expose the latest filing version at every observation time."""
    latest = filings.sort("known_from").group_by("sec_id", maintain_order=True).tail(1)
    return observations.join(latest, on="sec_id", how="left")


def assert_pit_matches_truth(
    pit_result: pl.DataFrame | pl.Series | _Sequence[_Any],
    reference_truth: pl.DataFrame | pl.Series | _Sequence[_Any] | _Any,
    *,
    columns: _Iterable[str] | None = None,
) -> None:
    """Assert the point-in-time result exactly matches the expected truth."""
    actual = _select_columns(pit_result, columns)
    expected = _select_columns(reference_truth, columns)
    if isinstance(actual, pl.DataFrame) and isinstance(expected, pl.DataFrame):
        _assert_frame_equal(actual, expected, check_dtypes=False)
    elif isinstance(actual, pl.Series) and isinstance(expected, pl.Series):
        _assert_series_equal(actual, expected, check_dtypes=False)
    else:
        assert actual == expected, f"PIT result differs from truth: {actual!r} != {expected!r}"


def assert_leaky_diverges(
    leaky_result: pl.DataFrame | pl.Series | _Sequence[_Any] | _Any,
    reference_truth: pl.DataFrame | pl.Series | _Sequence[_Any] | _Any,
    *,
    columns: _Iterable[str] | None = None,
) -> None:
    """Assert that a deliberately leaky result does not equal the truth."""
    actual = _select_columns(leaky_result, columns)
    expected = _select_columns(reference_truth, columns)
    if isinstance(actual, pl.DataFrame) and isinstance(expected, pl.DataFrame):
        try:
            _assert_frame_equal(actual, expected, check_dtypes=False)
        except AssertionError:
            return
    elif isinstance(actual, pl.Series) and isinstance(expected, pl.Series):
        try:
            _assert_series_equal(actual, expected, check_dtypes=False)
        except AssertionError:
            return
    elif actual != expected:
        return
    raise AssertionError("Leaky result unexpectedly matched the point-in-time truth")


def assert_no_lookahead(
    pipeline_result: pl.DataFrame | pl.Series | _Sequence[_Any] | _Any,
    reference_truth: pl.DataFrame | pl.Series | _Sequence[_Any] | _Any,
) -> None:
    """Assert a pipeline result contains no look-ahead relative to reference truth."""
    assert_pit_matches_truth(pipeline_result, reference_truth)


def _select_columns(value: _Any, columns: _Iterable[str] | None) -> _Any:
    if columns is None or not isinstance(value, pl.DataFrame):
        return value
    return value.select(list(columns))
