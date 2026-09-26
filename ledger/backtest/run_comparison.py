"""CLI entry point for the comparative leaky-versus-corrected backtest."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import polars as pl

from ledger.backtest.metrics import TRADING_DAYS_PER_YEAR
from ledger.backtest.runner import (
    load_yfinance_preadjusted_prices,
    run_comparison,
)
from ledger.backtest.simulation import SimulationConfig
from ledger.backtest.strategy import StrategyConfig
from ledger.backtest.tear_sheet import build_tear_sheet
from ledger.lineage.manifest import build_manifest, snapshot_input_files, write_manifest

LOGGER = logging.getLogger(__name__)
UTC = timezone.utc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledger run-comparison",
        description="Run Ledger's comparative backtest.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  ledger run-comparison --synthetic --start-date 2020-01-01 --end-date 2023-12-31\n"
            "  ledger run-comparison --start-date 2018-01-01 --end-date 2023-12-31\n"
            "  ledger run-comparison --start-date 2018-01-01 --end-date 2023-12-31 "
            "--tickers AAPL MSFT NVDA\n"
            "  ledger run-comparison --start-date 2018-01-01 --end-date 2023-12-31 "
            "--cost-bps 10 --rebalance-frequency weekly"
        ),
    )
    parser.add_argument(
        "--start-date",
        "--start",
        dest="start_date",
        required=True,
        help="Backtest start date (ISO format: YYYY-MM-DD).",
    )
    parser.add_argument(
        "--end-date",
        "--end",
        dest="end_date",
        required=True,
        help="Backtest end date (ISO format: YYYY-MM-DD).",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=["AAPL", "MSFT", "NVDA", "META", "GOOGL"],
        help="List of tickers to backtest (default: AAPL MSFT NVDA META GOOGL).",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=3,
        help="Number of top momentum stocks to hold (default: 3).",
    )
    parser.add_argument(
        "--cost-bps",
        type=float,
        default=5.0,
        help="Transaction cost in basis points (default: 5.0).",
    )
    parser.add_argument(
        "--initial-capital",
        type=float,
        default=1.0,
        help="Portfolio initial capital in dollars (default: 1.0).",
    )
    parser.add_argument(
        "--rebalance-frequency",
        choices=("daily", "weekly"),
        default="daily",
        help="Rebalancing frequency (default: daily).",
    )
    parser.add_argument(
        "--weekly-rebalance-day",
        type=int,
        default=0,
        help="Day of week for weekly rebalancing, 0=Monday (default: 0).",
    )
    parser.add_argument(
        "--synthetic",
        "--demo",
        dest="synthetic",
        action="store_true",
        help="Generate synthetic multi-ticker prices & corporate actions for offline demo.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("data/raw"),
        help="Root directory for raw market data (default: data/raw).",
    )
    parser.add_argument(
        "--prices-path",
        type=Path,
        help="Path to raw prices parquet file (overrides --data-root).",
    )
    parser.add_argument(
        "--preadjusted-prices-path",
        type=Path,
        help="Path to pre-adjusted prices parquet file (downloaded from yfinance if omitted).",
    )
    parser.add_argument(
        "--splits-path",
        type=Path,
        help="Path to corporate actions (splits) parquet file.",
    )
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=Path("artifacts/runs"),
        help="Root directory for output artifacts (default: artifacts/runs).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging output.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without writing artifacts.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return _run(args)
    except (OSError, ValueError) as error:
        LOGGER.error("%s", error)
        return 1


def _generate_synthetic_data(
    sec_ids: tuple[str, ...],
    start_date: date,
    end_date: date,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Generate deterministic synthetic price bars and corporate actions."""
    dates: list[date] = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            dates.append(current)
        current += timedelta(days=1)

    if not dates:
        raise ValueError("No weekday dates found within the specified date range.")

    raw_rows: list[dict[str, object]] = []
    preadj_rows: list[dict[str, object]] = []
    midpoint_date = dates[len(dates) // 2]
    split_sec_id = sec_ids[0]

    for offset, d in enumerate(dates):
        known_from = datetime.combine(d, time(21, 15), tzinfo=UTC)
        for i, sec_id in enumerate(sec_ids):
            base = 100.0 * (i + 1)
            slope = (i + 1) * 0.5
            current_level = base + slope * offset
            if sec_id == split_sec_id and d < midpoint_date:
                raw_close = current_level * 4.0
            else:
                raw_close = current_level

            preadj_close = current_level

            raw_rows.append(
                {
                    "sec_id": sec_id,
                    "trade_date": d,
                    "open": raw_close * 0.99,
                    "high": raw_close * 1.01,
                    "low": raw_close * 0.98,
                    "close": raw_close,
                    "volume": 1_000_000.0,
                    "known_from": known_from,
                }
            )
            preadj_rows.append(
                {
                    "sec_id": sec_id,
                    "trade_date": d,
                    "close": preadj_close,
                }
            )

    splits_rows = [
        {
            "sec_id": split_sec_id,
            "ex_date": midpoint_date,
            "split_ratio": 4.0,
            "known_from": datetime.combine(midpoint_date, time(21, 15), tzinfo=UTC),
        }
    ]

    return (
        pl.DataFrame(raw_rows),
        pl.DataFrame(preadj_rows),
        pl.DataFrame(
            splits_rows,
            schema={
                "sec_id": pl.String,
                "ex_date": pl.Date,
                "split_ratio": pl.Float64,
                "known_from": pl.Datetime("us", "UTC"),
            },
        ),
    )


def _run(args: argparse.Namespace) -> int:
    start_date = date.fromisoformat(args.start_date)
    end_date = date.fromisoformat(args.end_date)
    if end_date < start_date:
        raise ValueError("--end-date must be on or after --start-date.")
    tickers = _normalise_tickers(args.tickers)
    sec_ids = tuple(f"SEC_{ticker}_001" for ticker in tickers)

    if args.synthetic:
        LOGGER.info("Generating synthetic market data for %d tickers.", len(tickers))
        raw_prices, preadjusted, splits = _generate_synthetic_data(sec_ids, start_date, end_date)
        observation_matrix = _build_observations(raw_prices, sec_ids, start_date, end_date)
        input_snapshot = [
            {
                "path": "synthetic://deterministic-market-generator",
                "sha256": "sha256:synthetic_deterministic_market_fixture_v1",
                "size_bytes": len(raw_prices),
                "modified_time_utc": datetime.now(timezone.utc).isoformat(),
            }
        ]
    else:
        raw_source = args.prices_path or (args.data_root / "market_ohlcv")
        raw_prices = _read_parquet_source(raw_source, "raw prices")
        raw_prices = _filter_prices(raw_prices, sec_ids, start_date, end_date)
        _require_tickers(raw_prices, sec_ids, "raw prices")

        split_source = args.splits_path or (args.data_root / "corporate_actions")
        splits = _read_optional_splits(split_source, sec_ids, end_date)
        observation_matrix = _build_observations(raw_prices, sec_ids, start_date, end_date)

        preadjusted_source: Path | None = args.preadjusted_prices_path
        if preadjusted_source is not None:
            preadjusted = _filter_prices(
                _read_parquet_source(preadjusted_source, "preadjusted prices"),
                sec_ids,
                start_date,
                end_date,
            )
        else:
            LOGGER.info("Downloading pre-adjusted control prices from yfinance.")
            preadjusted = load_yfinance_preadjusted_prices(tickers, start_date, end_date)
        _require_tickers(preadjusted, sec_ids, "preadjusted prices")

        input_paths = [raw_source]
        if split_source.exists():
            input_paths.append(split_source)
        if preadjusted_source is not None:
            input_paths.append(preadjusted_source)
        input_snapshot = snapshot_input_files(input_paths)

    LOGGER.info("Running leaky and corrected pipelines for %d tickers.", len(tickers))
    comparison = run_comparison(
        observation_matrix=observation_matrix,
        raw_prices=raw_prices,
        preadjusted_prices=preadjusted,
        splits=splits,
        strategy_config=StrategyConfig(
            top_n=args.top_k,
            rebalance_frequency=args.rebalance_frequency,
            weekly_rebalance_day=args.weekly_rebalance_day,
        ),
        simulation_config=SimulationConfig(
            transaction_cost_bps=args.cost_bps,
            initial_capital=args.initial_capital,
        ),
        leaky_universe=tuple(tickers),
    )
    periods_per_year = 52 if args.rebalance_frequency == "weekly" else TRADING_DAYS_PER_YEAR
    tear_sheet = build_tear_sheet(
        comparison.leaky.simulation,
        comparison.corrected.simulation,
        periods_per_year=periods_per_year,
        initial_capital=args.initial_capital,
    )
    print(tear_sheet.render())

    artifact_names = {
        "equity_leaky": "equity_leaky.parquet",
        "equity_corrected": "equity_corrected.parquet",
        "returns_leaky": "returns_leaky.parquet",
        "returns_corrected": "returns_corrected.parquet",
        "weights_leaky": "weights_leaky.parquet",
        "weights_corrected": "weights_corrected.parquet",
    }
    results = {
        "leaky": tear_sheet.leaky.as_dict(),
        "corrected": tear_sheet.corrected.as_dict(),
        "delta": tear_sheet.delta.as_dict(),
        "artifacts": artifact_names,
    }
    manifest = build_manifest(
        input_snapshot=input_snapshot,
        feature_names=["adj_close", "momentum_20d", "volatility_20d", "sma_50d"],
        parameters={
            "start_date": args.start_date,
            "end_date": args.end_date,
            "universe": list(sec_ids),
            "top_k": args.top_k,
            "cost_bps": args.cost_bps,
            "initial_capital": args.initial_capital,
            "rebalance_frequency": args.rebalance_frequency,
            "periods_per_year": periods_per_year,
        },
        results=results,
    )
    run_dir = args.artifacts_root / manifest["run_id"]
    manifest["audit"]["output_directory"] = str(run_dir)
    if args.dry_run:
        print(f"Dry run: no artifacts written. Run ID: {manifest['run_id']}")
        return 0

    run_dir.mkdir(parents=True, exist_ok=True)
    comparison.leaky.simulation.write_parquet(run_dir / artifact_names["returns_leaky"])
    comparison.corrected.simulation.write_parquet(run_dir / artifact_names["returns_corrected"])
    _write_equity(comparison.leaky.simulation, run_dir / artifact_names["equity_leaky"])
    _write_equity(comparison.corrected.simulation, run_dir / artifact_names["equity_corrected"])
    comparison.leaky.weights.write_parquet(run_dir / artifact_names["weights_leaky"])
    comparison.corrected.weights.write_parquet(run_dir / artifact_names["weights_corrected"])
    manifest_path = write_manifest(manifest, args.artifacts_root)

    pdf_path = run_dir / "report.pdf"
    try:
        from ledger.backtest.pdf_report import generate_pdf_report

        generate_pdf_report(
            output_path=pdf_path,
            run_id=manifest["run_id"],
            leaky_metrics=tear_sheet.leaky,
            corrected_metrics=tear_sheet.corrected,
            leaky_equity=comparison.leaky.simulation.rename({"observation_timestamp": "timestamp"}),
            corrected_equity=comparison.corrected.simulation.rename(
                {"observation_timestamp": "timestamp"}
            ),
            manifest_data=manifest,
        )
        print(f"PDF Report: {pdf_path}")
    except Exception as pdf_err:
        LOGGER.warning("Could not generate PDF report: %s", pdf_err)

    print(f"Run ID: {manifest['run_id']}")
    print(f"Manifest: {manifest_path}")
    return 0


def _read_parquet_source(source: Path, label: str) -> pl.DataFrame:
    files = _parquet_files(source)
    if not files:
        hint = (
            f"\n\nHint: Storage directory '{source}' contains no data partitions.\n"
            "  - Run 'python scripts/seed_week1.py' to download data,\n"
            "  - Or run with '--synthetic' for an instant offline demonstration backtest."
            if "raw prices" in label
            else ""
        )
        raise ValueError(f"No Parquet files found for {label}: {source}{hint}")
    return pl.concat([pl.read_parquet(file) for file in files], how="diagonal_relaxed")


def _read_optional_splits(source: Path, sec_ids: tuple[str, ...], end_date: date) -> pl.DataFrame:
    if not source.exists():
        return pl.DataFrame(
            schema={
                "sec_id": pl.String,
                "ex_date": pl.Date,
                "split_ratio": pl.Float64,
                "known_from": pl.Datetime("us", "UTC"),
            }
        )
    splits = _read_parquet_source(source, "corporate actions")
    if "action_type" in splits.columns:
        splits = splits.filter(pl.col("action_type").str.to_uppercase() == "SPLIT")
    return splits.filter(
        pl.col("sec_id").is_in(sec_ids) & (pl.col("ex_date") <= pl.lit(end_date))
    ).select(["sec_id", "ex_date", "split_ratio", "known_from"])


def _filter_prices(
    prices: pl.DataFrame,
    sec_ids: tuple[str, ...],
    start_date: date,
    end_date: date,
) -> pl.DataFrame:
    return prices.filter(
        pl.col("sec_id").is_in(sec_ids)
        & (pl.col("trade_date") >= pl.lit(start_date))
        & (pl.col("trade_date") <= pl.lit(end_date))
    )


def _build_observations(
    prices: pl.DataFrame,
    sec_ids: tuple[str, ...],
    start_date: date,
    end_date: date,
) -> pl.DataFrame:
    dates = (
        prices.filter(
            (pl.col("trade_date") >= pl.lit(start_date))
            & (pl.col("trade_date") <= pl.lit(end_date))
        )
        .select("trade_date")
        .unique()
        .sort("trade_date")["trade_date"]
        .to_list()
    )
    if not dates:
        raise ValueError("No trading dates remain after applying --start-date and --end-date.")
    timestamps = [datetime.combine(day, time(21, 5), tzinfo=UTC) for day in dates]
    return pl.DataFrame(
        {
            "sec_id": [sec_id for sec_id in sec_ids for _ in timestamps],
            "observation_timestamp": timestamps * len(sec_ids),
        }
    )


def _require_tickers(prices: pl.DataFrame, sec_ids: tuple[str, ...], label: str) -> None:
    found = set(prices.get_column("sec_id").unique().to_list())
    missing = sorted(set(sec_ids) - found)
    if missing:
        raise ValueError(f"{label} missing requested ticker data: {missing}")


def _normalise_tickers(tickers: list[str]) -> list[str]:
    normalised = [ticker.strip().upper() for ticker in tickers]
    if not normalised or any(not ticker for ticker in normalised):
        raise ValueError("--tickers must contain at least one non-empty ticker.")
    if len(set(normalised)) != len(normalised):
        raise ValueError("--tickers must not contain duplicates.")
    return normalised


def _parquet_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source] if source.suffix.lower() == ".parquet" else []
    if source.is_dir():
        return sorted(path for path in source.rglob("*.parquet") if path.is_file())
    return []


def _write_equity(simulation: pl.DataFrame, path: Path) -> None:
    simulation.select(["observation_timestamp", "equity"]).write_parquet(path)


if __name__ == "__main__":
    raise SystemExit(main())
