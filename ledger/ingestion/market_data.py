"""Market data ingestion pipeline for raw daily OHLCV bars.

Critical Invariants:
1. Prices are stored **as traded**, i.e. the unadjusted historical print. yfinance
   returns a series split-adjusted over its *entire* history, so a row dated
   before a later split arrives pre-scaled by that later split. That is
   look-ahead sitting in a table named "raw", and it is the precise defect this
   project exists to prevent. `_undo_full_history_split_adjustment` reverses the
   adjustment using the split history, so a stored price depends only on events
   at or before its own date. See ADR 006 and the `raw` price-basis invariant in
   `tests/unit/test_ingestion.py`.
2. Tickers are mapped to synthetic permanent `sec_id` (SEC_<TICKER>_001).
3. `known_from` is computed via exchange calendar session close + 15 min buffer (16:15 EST).
4. Atomic Parquet writing and monotonic `ingestion_seq` audit logging.
"""

from __future__ import annotations

import argparse
import contextlib
from collections.abc import Sequence
from datetime import date, datetime, time, timezone
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


def _undo_full_history_split_adjustment(
    df: pl.DataFrame,
    splits: pl.DataFrame | None,
) -> pl.DataFrame:
    """Restore as-traded price levels by undoing the provider's full-history adjustment.

    yfinance returns a price series scaled so that *every* bar is expressed in
    current-share terms. The scale factor applied to a bar dated ``t`` is the
    product of every split ratio whose ex-date is strictly after ``t``. TSLA on
    2020-08-28 is the worked example: the true print was ~$2,213, but the
    provider returns 2213 / 5 (2020-08-31) / 3 (2022-08-25) = 147.56, so the
    2022 split is embedded in a 2020 row.

    Multiplying each bar back by its own factor restores the as-traded level:

        as_traded[t] = provider[t] * prod(ratio for splits with ex_date > t)

    This makes a stored price a function only of events at or before ``t``, which
    is what a point-in-time store requires. The adjusted series is still
    recoverable, and is what the dynamic CAF feature recomputes from the split
    table at query time.

    Volume is deliberately left alone: the provider already reports volume in
    as-traded share units (no step appears across an ex-date), so rescaling it
    here would corrupt it.
    """
    if splits is None or splits.is_empty():
        return df

    price_columns = ["open", "high", "low", "close"]
    if not set(price_columns).issubset(df.columns):
        return df

    unique_splits = splits.select(["sec_id", "ex_date", "split_ratio"]).unique(
        subset=["sec_id", "ex_date", "split_ratio"]
    )

    out: list[pl.DataFrame] = []
    for sec_key, group in df.group_by("sec_id", maintain_order=True):
        # Polars yields the grouping key as a 1-tuple; unwrap it so the
        # comparison below is against a scalar rather than a list.
        sec_id = sec_key[0] if isinstance(sec_key, tuple) else sec_key
        sec_splits = unique_splits.filter(pl.col("sec_id") == sec_id)
        if sec_splits.is_empty():
            out.append(group)
            continue
        # ex_dates strictly after each trade_date, as a running product.
        factors = (
            group.select("trade_date")
            .join(
                sec_splits.rename({"ex_date": "__ex_date"}),
                how="cross",
            )
            .with_columns(
                pl.when(pl.col("__ex_date") > pl.col("trade_date"))
                .then(pl.col("split_ratio"))
                .otherwise(1.0)
                .alias("__factor")
            )
            .group_by("trade_date", maintain_order=True)
            .agg(pl.col("__factor").product().alias("__factor"))
        )
        out.append(
            group.join(factors, on="trade_date", how="left")
            .with_columns(
                [pl.col(col).cast(pl.Float64) * pl.col("__factor") for col in price_columns]
            )
            .drop("__factor")
        )

    return pl.concat(out, how="vertical")


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
    splits: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Parse raw yfinance DataFrame into canonical fact_market_ohlcv_raw schema.

    Computes `known_from` timestamp using exchange session close + buffer.

    Price basis: stored levels are **as traded**, not the provider's
    full-history-adjusted series. ``splits`` must carry the security's split
    history so the provider's adjustment can be undone; without it the function
    would store a price that already embeds later corporate actions. See
    `_undo_full_history_split_adjustment` and the module docstring.
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

        # Actionable timestamp: session close + buffer on trade_d, in UTC.
        #
        # Anchor at midday New York, NOT naive midnight. `_to_ny_datetime`
        # treats a naive value as UTC, so midnight on trade_d converts to
        # 20:00 the PREVIOUS day in New York. The calendar then sees a
        # non-session date and walks back to the previous session close,
        # stamping every bar a session early. For 2020-08-31 that meant
        # known_from = 2020-08-28 20:15 UTC, which made the 4:1 ex-date bar
        # visible a full session before it printed -- look-ahead, and enough
        # to shift the ex-date pairing in the simulator.
        trade_dt = datetime.combine(trade_d, time(hour=12))
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

    out = pl.DataFrame(
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
    return _undo_full_history_split_adjustment(out, splits)


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
        # auto_adjust=False yields 'Close' (split-adjusted over the FULL history,
        # not dividend-adjusted) and 'Adj Close' (split + dividend adjusted).
        # We read 'Close' and then undo its split adjustment, because the
        # full-history basis embeds splits that post-date the bar -- look-ahead
        # in a table named "raw".
        t_obj = yf.Ticker(clean_ticker)
        history_kwargs: dict[str, Any] = {
            "start": start_str,
            "auto_adjust": False,
            "actions": False,
            "repair": False,
        }
        if end_str:
            history_kwargs["end"] = end_str

        # The FULL split history is required here, including splits after
        # end_date: those are exactly the factors the provider has already
        # baked into the bars we are about to download.
        history_splits = t_obj.splits
        split_df: pl.DataFrame | None = None
        if history_splits is not None and not history_splits.empty:
            split_df = pl.DataFrame(
                {
                    "sec_id": pl.Series([sec_id] * len(history_splits), dtype=pl.String),
                    "ex_date": pl.Series(
                        [_extract_date(i) for i in history_splits.index], dtype=pl.Date
                    ),
                    "split_ratio": pl.Series(
                        [float(v) for v in history_splits.to_numpy()], dtype=pl.Float64
                    ),
                }
            )

        raw_df = t_obj.history(**history_kwargs)
        if raw_df is not None and not raw_df.empty:
            parsed_df = parse_yfinance_ohlcv_dataframe(
                raw_df=raw_df,
                ticker=clean_ticker,
                sec_id=sec_id,
                ingestion_seq=ingestion_seq,
                splits=split_df,
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
