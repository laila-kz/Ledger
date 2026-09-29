"""Corporate actions ingestion pipeline for stock splits and cash dividends.

Critical Invariants:
1. Splits and dividends are stored as distinct append-only fact records.
2. `known_from` convention: Defaults to `ex_date` session close + 15 min buffer (16:15 EST),
   ensuring backtest strategies cannot consume split/dividend knowledge before market execution.
3. Monotonic sequence and yearly Hive Parquet partitioning (/year=YYYY/).
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl
import yfinance as yf

from ledger.core.calendars import get_actionable_timestamp
from ledger.ingestion.market_data import TickerRegistry
from ledger.storage.ingestion_log import IngestionLogEntry, IngestionLogManager
from ledger.storage.partitions import write_partitioned_corporate_actions


def _extract_date(dt_val: Any) -> date:
    """Extract standard date object from timestamp or date representation."""
    if isinstance(dt_val, (pd.Timestamp, datetime)):
        return dt_val.date()
    if isinstance(dt_val, date):
        return dt_val
    return pd.to_datetime(str(dt_val)).date()


def parse_splits_series(
    splits_series: pd.Series[Any],
    sec_id: str,
    ingestion_seq: int,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict[str, object]]:
    """Convert yfinance splits Series into structured corporate actions records."""
    records: list[dict[str, object]] = []
    if splits_series is None or splits_series.empty:
        return records

    for dt_index, ratio_val in splits_series.items():
        ex_d = _extract_date(dt_index)

        if start_date and ex_d < start_date:
            continue
        if end_date and ex_d > end_date:
            continue

        ratio = float(ratio_val)
        if ratio <= 0:
            continue

        # Anchor at midday New York, not naive midnight: `_to_ny_datetime`
        # reads a naive value as UTC, so midnight resolves to the previous day
        # in New York and the calendar walks back a session. See the same note
        # in market_data.parse_yfinance_ohlcv_dataframe.
        ex_dt = datetime.combine(ex_d, time(hour=12))
        actionable_ny = get_actionable_timestamp(ex_dt, is_market_data=True)
        known_from_utc = actionable_ny.astimezone(timezone.utc)

        records.append(
            {
                # sec_id must be carried here, not just passed in: the split
                # table is the join key between corporate actions and prices, so
                # a null sec_id silently detaches every split from its security
                # and the CAF has nothing to apply.
                "sec_id": sec_id,
                "action_type": "SPLIT",
                "ex_date": ex_d,
                "split_ratio": ratio,
                "cash_amount": None,
                "announcement_date": ex_d,
                "known_from": known_from_utc,
                "ingestion_seq": ingestion_seq,
            }
        )

    return records


def parse_dividends_series(
    divs_series: pd.Series[Any],
    sec_id: str,
    ingestion_seq: int,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict[str, object]]:
    """Convert yfinance dividends Series into structured corporate actions records."""
    records: list[dict[str, object]] = []
    if divs_series is None or divs_series.empty:
        return records

    for dt_index, amount_val in divs_series.items():
        ex_d = _extract_date(dt_index)

        if start_date and ex_d < start_date:
            continue
        if end_date and ex_d > end_date:
            continue

        amount = float(amount_val)
        if amount <= 0:
            continue

        # Midday anchor for the same reason as the split path above.
        ex_dt = datetime.combine(ex_d, time(hour=12))
        actionable_ny = get_actionable_timestamp(ex_dt, is_market_data=True)
        known_from_utc = actionable_ny.astimezone(timezone.utc)

        records.append(
            {
                "sec_id": sec_id,
                "action_type": "CASH_DIVIDEND",
                "ex_date": ex_d,
                "split_ratio": None,
                "cash_amount": amount,
                "announcement_date": ex_d,
                "known_from": known_from_utc,
                "ingestion_seq": ingestion_seq,
            }
        )

    return records


def ingest_corporate_actions(
    tickers: Sequence[str],
    start_date: str | date = "2020-01-01",
    end_date: str | date | None = None,
    base_dir: Path | str = "data/raw",
    source: str = "yfinance",
    registry: TickerRegistry | None = None,
) -> IngestionLogEntry | None:
    """Ingest stock splits and cash dividends and write to partitioned Parquet (/year=YYYY/).

    Args:
        tickers: List of ticker symbols.
        start_date: Start date string or date object.
        end_date: End date string or date object.
        base_dir: Root storage directory.
        source: Source descriptor.
        registry: Optional TickerRegistry instance.

    Returns:
        Committed IngestionLogEntry or None if no records found.
    """
    base_path = Path(base_dir)
    reg = registry or TickerRegistry(base_dir=base_path)
    log_mgr = IngestionLogManager(base_dir=base_path)
    ingestion_seq = log_mgr.get_next_seq()

    start_d = date.fromisoformat(start_date) if isinstance(start_date, str) else start_date
    end_d = (
        (date.fromisoformat(end_date) if isinstance(end_date, str) else end_date)
        if end_date
        else None
    )

    all_records: list[dict[str, object]] = []

    for ticker in tickers:
        clean_ticker = ticker.strip().upper()
        sec_id = reg.get_or_create_sec_id(clean_ticker)

        t_obj = yf.Ticker(clean_ticker)

        # Ingest Splits
        splits = t_obj.splits
        if splits is not None and not splits.empty:
            split_recs = parse_splits_series(
                splits_series=splits,
                sec_id=sec_id,
                ingestion_seq=ingestion_seq,
                start_date=start_d,
                end_date=end_d,
            )
            all_records.extend(split_recs)

        # Ingest Dividends
        divs = t_obj.dividends
        if divs is not None and not divs.empty:
            div_recs = parse_dividends_series(
                divs_series=divs,
                sec_id=sec_id,
                ingestion_seq=ingestion_seq,
                start_date=start_d,
                end_date=end_d,
            )
            all_records.extend(div_recs)

    if not all_records:
        return None

    df = pl.DataFrame(
        all_records,
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

    written_files = write_partitioned_corporate_actions(
        df=df,
        base_dir=base_path,
        ingestion_seq=ingestion_seq,
    )

    return log_mgr.record_batch(
        table_name="corporate_actions",
        source=source,
        files_metadata=written_files,
        ingestion_seq=ingestion_seq,
    )


def main() -> None:
    """CLI entry point for running corporate actions ingestion."""
    parser = argparse.ArgumentParser(description="Ledger Corporate Actions Ingestion Pipeline")
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=["AAPL", "TSLA", "NVDA", "MSFT", "GOOGL"],
        help="List of tickers to ingest",
    )
    parser.add_argument("--start", default="2020-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--base-dir", default="data/raw", help="Root data directory")

    args = parser.parse_args()
    print(f"Starting corporate actions ingestion ({args.start} to {args.end})...")
    entry = ingest_corporate_actions(
        tickers=args.tickers,
        start_date=args.start,
        end_date=args.end,
        base_dir=args.base_dir,
    )
    if entry:
        print(
            f"Successfully committed Batch #{entry.ingestion_seq}: "
            f"{entry.row_count} actions across {entry.file_count} partitions."
        )
    else:
        print("No corporate actions found for the specified period.")


if __name__ == "__main__":
    main()
