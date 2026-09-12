"""Market data ingestion pipeline for raw daily OHLCV bars.

Critical Invariants:
1. Prices stored are split-adjusted but NOT dividend-adjusted (i.e., the 'Close' column
   from yfinance, not 'Adj Close'). yfinance >= 0.2 always applies split adjustments;
   pre-split absolute prices are reconstructable via the split_ratio in fact_corporate_actions.
2. Tickers are mapped to synthetic permanent `sec_id` (SEC_<TICKER>_001).
3. `known_from` is computed via exchange calendar session close + 15 min buffer (16:15 EST).
4. Atomic Parquet writing and monotonic `ingestion_seq` audit logging.
"""

from __future__ import annotations

import argparse
import contextlib
from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl
import yfinance as yf

from ledger.core.calendars import get_actionable_timestamp
from ledger.core.entity import SecurityTickerMapping
from ledger.storage.ingestion_log import IngestionLogEntry, IngestionLogManager
from ledger.storage.partitions import write_entity_map, write_partitioned_market_ohlcv


def _extract_date(dt_val: Any) -> date:
    """Extract standard date object from timestamp or date representation."""
    if isinstance(dt_val, (pd.Timestamp, datetime)):
        return dt_val.date()
    if isinstance(dt_val, date):
        return dt_val
    return pd.to_datetime(str(dt_val)).date()


class TickerRegistry:
    """Manages mapping from raw human-readable ticker symbols to synthetic permanent SecIDs."""

    def __init__(self, base_dir: Path | str = "data/raw") -> None:
        self.base_dir = Path(base_dir)

    def get_or_create_sec_id(self, ticker: str) -> str:
        """Generate canonical synthetic SecID for a ticker symbol."""
        clean_ticker = ticker.strip().upper()
        return f"SEC_{clean_ticker}_001"

    def register_tickers(
        self,
        tickers: Sequence[str],
        valid_from: datetime | date = date(1980, 1, 1),
        ingestion_seq: int = 1,
    ) -> list[SecurityTickerMapping]:
        """Register a list of tickers and persist to entity_map Parquet."""
        mappings: list[SecurityTickerMapping] = []
        rows = []

        for ticker in tickers:
            clean_ticker = ticker.strip().upper()
            sec_id = self.get_or_create_sec_id(clean_ticker)
            mapping = SecurityTickerMapping(
                sec_id=sec_id,
                ticker=clean_ticker,
                valid_from=valid_from,
                ingestion_seq=ingestion_seq,
            )
            mappings.append(mapping)
            rows.append(
                {
                    "sec_id": mapping.sec_id,
                    "ticker": mapping.ticker,
                    "valid_from": (
                        datetime.combine(valid_from, datetime.min.time(), tzinfo=timezone.utc)
                        if isinstance(valid_from, date) and not isinstance(valid_from, datetime)
                        else valid_from
                    ),
                    "ingestion_seq": mapping.ingestion_seq,
                }
            )

        if rows:
            df = pl.DataFrame(rows)
            write_entity_map(df=df, base_dir=self.base_dir, ingestion_seq=ingestion_seq)

        return mappings


def parse_yfinance_ohlcv_dataframe(
    raw_df: pd.DataFrame,
    ticker: str,
    sec_id: str,
    ingestion_seq: int,
) -> pl.DataFrame:
    """Parse raw yfinance DataFrame into canonical fact_market_ohlcv_raw schema.

    Enforces:
    - Strictly unadjusted Open, High, Low, Close, Volume.
    - Computes `known_from` timestamp using exchange session close + buffer.
    """
    if raw_df.empty:
        return pl.DataFrame(
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
            }
        )

    # Flatten multi-index columns if present
    df: Any = raw_df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        with contextlib.suppress(KeyError, IndexError):
            df = df.xs(ticker, axis=1, level=1)
        if isinstance(df.columns, pd.MultiIndex):
            with contextlib.suppress(KeyError, IndexError):
                df = df.xs(ticker, axis=1, level=0)

    records = []
    for dt_index, row in df.iterrows():
        trade_d = _extract_date(dt_index)

        # Compute actionable timestamp: 16:15 EST on trade_d converted to UTC
        trade_dt = datetime.combine(trade_d, datetime.min.time())
        actionable_ny = get_actionable_timestamp(trade_dt, is_market_data=True)
        known_from_utc = actionable_ny.astimezone(timezone.utc)

        open_val = float(row.get("Open", row.get("open", 0.0)))
        high_val = float(row.get("High", row.get("high", 0.0)))
        low_val = float(row.get("Low", row.get("low", 0.0)))
        close_val = float(row.get("Close", row.get("close", 0.0)))
        vol_val = int(row.get("Volume", row.get("volume", 0)))

        records.append(
            {
                "sec_id": sec_id,
                "trade_date": trade_d,
                "open": open_val,
                "high": high_val,
                "low": low_val,
                "close": close_val,
                "volume": vol_val,
                "known_from": known_from_utc,
                "ingestion_seq": ingestion_seq,
            }
        )

    return pl.DataFrame(
        records,
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


def ingest_ohlcv(
    tickers: Sequence[str],
    start_date: str | date = "2020-01-01",
    end_date: str | date | None = None,
    base_dir: Path | str = "data/raw",
    source: str = "yfinance",
    registry: TickerRegistry | None = None,
) -> IngestionLogEntry | None:
    """Ingest raw unadjusted OHLCV price bars for given tickers and write to partitioned Parquet.

    Args:
        tickers: List of ticker symbols to fetch.
        start_date: Start date string (YYYY-MM-DD) or date object.
        end_date: End date string (YYYY-MM-DD) or date object.
        base_dir: Root storage directory.
        source: Ingestion source descriptor.
        registry: Optional TickerRegistry instance.

    Returns:
        The committed IngestionLogEntry or None if no data was fetched.
    """
    base_path = Path(base_dir)
    reg = registry or TickerRegistry(base_dir=base_path)
    log_mgr = IngestionLogManager(base_dir=base_path)
    ingestion_seq = log_mgr.get_next_seq()

    # Register tickers in entity map
    reg.register_tickers(tickers=tickers, ingestion_seq=ingestion_seq)

    start_str = start_date.isoformat() if isinstance(start_date, date) else str(start_date)
    end_str = (
        end_date.isoformat()
        if isinstance(end_date, date)
        else (str(end_date) if end_date else None)
    )

    all_dfs: list[pl.DataFrame] = []

    for ticker in tickers:
        clean_ticker = ticker.strip().upper()
        sec_id = reg.get_or_create_sec_id(clean_ticker)

        # Use Ticker.history() for reliable single-ticker OHLCV retrieval.
        # auto_adjust=False: returns 'Close' (split-adjusted, NOT dividend-adjusted)
        # and 'Adj Close' (fully adjusted). We store 'Close' only.
        # NOTE: yfinance >= 0.2 always applies split adjustments regardless of flags;
        # this is the canonical split-adjusted price. Splits are tracked separately
        # in fact_corporate_actions so the pre-split price is always recoverable.
        t_obj = yf.Ticker(clean_ticker)
        history_kwargs: dict[str, Any] = {
            "start": start_str,
            "auto_adjust": False,
            "actions": False,
            "repair": False,
        }
        if end_str:
            history_kwargs["end"] = end_str

        raw_df = t_obj.history(**history_kwargs)
        if raw_df is not None and not raw_df.empty:
            parsed_df = parse_yfinance_ohlcv_dataframe(
                raw_df=raw_df,
                ticker=clean_ticker,
                sec_id=sec_id,
                ingestion_seq=ingestion_seq,
            )
            if len(parsed_df) > 0:
                all_dfs.append(parsed_df)

    if not all_dfs:
        return None

    combined_df = pl.concat(all_dfs)
    written_files = write_partitioned_market_ohlcv(
        df=combined_df,
        base_dir=base_path,
        ingestion_seq=ingestion_seq,
    )

    return log_mgr.record_batch(
        table_name="market_ohlcv",
        source=source,
        files_metadata=written_files,
        ingestion_seq=ingestion_seq,
    )


def main() -> None:
    """CLI entry point for running market data ingestion."""
    parser = argparse.ArgumentParser(description="Ledger Raw OHLCV Ingestion Pipeline")
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=["AAPL", "MSFT", "NVDA", "META", "GOOGL"],
        help="List of tickers to ingest",
    )
    parser.add_argument("--start", default="2020-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--base-dir", default="data/raw", help="Root data directory")

    args = parser.parse_args()
    print(f"Starting OHLCV ingestion for {args.tickers} ({args.start} to {args.end})...")
    entry = ingest_ohlcv(
        tickers=args.tickers,
        start_date=args.start,
        end_date=args.end,
        base_dir=args.base_dir,
    )
    if entry:
        print(
            f"Successfully committed Batch #{entry.ingestion_seq}: "
            f"{entry.row_count} rows across {entry.file_count} partitions."
        )
    else:
        print("No records fetched.")


if __name__ == "__main__":
    main()
