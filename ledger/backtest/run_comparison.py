"""CLI entry point for the comparative leaky-versus-corrected backtest."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, time, timezone
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
    parser = argparse.ArgumentParser(description="Run Ledger's comparative backtest.")
    parser.add_argument("--start-date", "--start", dest="start_date", required=True)
    parser.add_argument("--end-date", "--end", dest="end_date", required=True)
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=["AAPL", "MSFT", "NVDA", "META", "GOOGL"],
    )
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--cost-bps", type=float, default=5.0)
    parser.add_argument("--initial-capital", type=float, default=1.0)
    parser.add_argument(
        "--rebalance-frequency", choices=("daily", "weekly"), default="daily"
    )
    parser.add_argument("--weekly-rebalance-day", type=int, default=0)
    parser.add_argument("--data-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--prices-path", type=Path)
    parser.add_argument("--preadjusted-prices-path", type=Path)
    parser.add_argument("--splits-path", type=Path)
    parser.add_argument("--artifacts-root", type=Path, default=Path("artifacts/runs"))
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
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


def _run(args: argparse.Namespace) -> int:
    start_date = date.fromisoformat(args.start_date)
    end_date = date.fromisoformat(args.end_date)
    if end_date < start_date:
        raise ValueError("--end-date must be on or after --start-date.")
    tickers = _normalise_tickers(args.tickers)
    sec_ids = tuple(f"SEC_{ticker}_001" for ticker in tickers)

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
        leaky_universe=tickers,
    )
    periods_per_year = (
        52 if args.rebalance_frequency == "weekly" else TRADING_DAYS_PER_YEAR
    )
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
    print(f"Run ID: {manifest['run_id']}")
    print(f"Manifest: {manifest_path}")
    return 0


def _read_parquet_source(source: Path, label: str) -> pl.DataFrame:
    files = _parquet_files(source)
    if not files:
        raise ValueError(f"No Parquet files found for {label}: {source}")
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
