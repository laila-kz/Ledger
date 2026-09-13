"""Standalone benchmark: Vectorized ASOF join engine throughput.

Usage:
    uv run python benchmarks/bench_asof_engine.py
    uv run python benchmarks/bench_asof_engine.py --tickers 20 --obs 200 --runs 5

Benchmark measures WARM-path performance only (cold first call excluded).
Outputs a table of (scale, mean_ms, p99_ms, rows/s) for review.
"""

from __future__ import annotations

import argparse
import sys
import time
import zoneinfo
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Ensure repo root is on sys.path for direct execution
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import polars as pl  # noqa: E402

from ledger.features.engine import join_features_as_of  # noqa: E402

UTC = zoneinfo.ZoneInfo("UTC")

# ---------------------------------------------------------------------------
# Dataset builders
# ---------------------------------------------------------------------------


def build_feature_df(
    sec_ids: list[str],
    num_bars: int,
    base_dt: datetime,
) -> pl.DataFrame:
    """Build a synthetic OHLCV price feature table."""
    rows: list[dict[str, Any]] = []
    for sec in sec_ids:
        for day in range(num_bars):
            rows.append(
                {
                    "sec_id": sec,
                    "close": 100.0 + day * 0.1,
                    "volume": int(1_000_000 + day * 100),
                    "known_from": base_dt + timedelta(days=day),
                }
            )
    return pl.DataFrame(rows)


def build_observation_matrix(
    sec_ids: list[str],
    obs_per_ticker: int,
    base_dt: datetime,
) -> pl.DataFrame:
    """Build an observation matrix with obs_per_ticker queries per security."""
    rows: list[dict[str, Any]] = []
    for sec in sec_ids:
        for i in range(obs_per_ticker):
            rows.append(
                {
                    "sec_id": sec,
                    "observation_timestamp": base_dt + timedelta(days=i * 2, hours=10),
                }
            )
    return pl.DataFrame(rows)


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------


def run_benchmark(
    num_tickers: int = 10,
    obs_per_ticker: int = 100,
    num_bars: int = 1_000,
    warmup_runs: int = 2,
    timed_runs: int = 5,
) -> dict[str, Any]:
    """Run the ASOF join benchmark and return timing statistics."""
    base_dt = datetime(2022, 1, 1, 21, 0, tzinfo=UTC)
    sec_ids = [f"SEC_{i:04d}" for i in range(num_tickers)]
    total_obs = num_tickers * obs_per_ticker
    total_feature_rows = num_tickers * num_bars

    feature_df = build_feature_df(sec_ids, num_bars, base_dt)
    entity_df = build_observation_matrix(sec_ids, obs_per_ticker, base_dt)

    # Warm-up: excluded from timing measurement
    for _ in range(warmup_runs):
        _ = join_features_as_of(entity_df, [feature_df])

    # Timed runs
    durations_ms: list[float] = []
    for _ in range(timed_runs):
        t0 = time.perf_counter()
        result = join_features_as_of(entity_df, [feature_df])
        durations_ms.append((time.perf_counter() - t0) * 1000.0)

    durations_ms.sort()
    mean_ms = sum(durations_ms) / len(durations_ms)
    p50_ms = durations_ms[len(durations_ms) // 2]
    p99_ms = durations_ms[-1]
    rows_per_sec = (total_obs / mean_ms) * 1000.0

    assert result.height == total_obs, "Row count mismatch!"

    return {
        "num_tickers": num_tickers,
        "obs_per_ticker": obs_per_ticker,
        "total_obs": total_obs,
        "total_feature_rows": total_feature_rows,
        "warmup_runs": warmup_runs,
        "timed_runs": timed_runs,
        "mean_ms": mean_ms,
        "p50_ms": p50_ms,
        "p99_ms": p99_ms,
        "rows_per_sec": rows_per_sec,
        "under_100ms": mean_ms < 100.0,
    }


def print_results(stats: dict[str, Any]) -> None:
    """Print a formatted benchmark result table."""
    print()
    print("=" * 60)
    print("  Ledger ASOF Join Engine Benchmark")
    print("=" * 60)
    print(f"  Tickers          : {stats['num_tickers']}")
    print(f"  Obs per ticker   : {stats['obs_per_ticker']}")
    print(f"  Total observations: {stats['total_obs']:,}")
    print(f"  Total feature rows: {stats['total_feature_rows']:,}")
    print(f"  Warm-up runs     : {stats['warmup_runs']}")
    print(f"  Timed runs       : {stats['timed_runs']}")
    print("-" * 60)
    print(f"  Mean latency     : {stats['mean_ms']:.2f} ms")
    print(f"  P50 latency      : {stats['p50_ms']:.2f} ms")
    print(f"  P99 latency      : {stats['p99_ms']:.2f} ms")
    print(f"  Throughput       : {stats['rows_per_sec']:,.0f} obs/sec")
    sla = "PASS (<100ms)" if stats["under_100ms"] else "FAIL (>=100ms)"
    print(f"  SLA (<100ms)     : {sla}")
    print("=" * 60)
    print()


# ---------------------------------------------------------------------------
# Multi-scale sweep
# ---------------------------------------------------------------------------

SCALE_MATRIX = [
    # (num_tickers, obs_per_ticker, num_bars)  -- total_obs shown in comment
    (5, 200, 1_000),  # 1,000 obs  -- baseline
    (10, 200, 1_000),  # 2,000 obs  -- 2x scale
    (20, 100, 1_000),  # 2,000 obs  -- wider not deeper
    (50, 50, 1_000),  # 2,500 obs  -- many tickers
    (100, 30, 1_000),  # 3,000 obs  -- stress
]


def run_scale_sweep() -> None:
    """Run the benchmark across multiple (tickers, observations) combinations."""
    print()
    print("Ledger Vectorized ASOF Engine - Scale Sweep")
    print("=" * 78)
    header = (
        f"{'Tickers':>8} {'Obs/Ticker':>10} {'Total Obs':>10}"
        f" {'Mean ms':>10} {'P99 ms':>8} {'SLA':>8}"
    )
    print(header)
    print("-" * 78)

    for num_tickers, obs_per_ticker, num_bars in SCALE_MATRIX:
        stats = run_benchmark(
            num_tickers=num_tickers,
            obs_per_ticker=obs_per_ticker,
            num_bars=num_bars,
            warmup_runs=2,
            timed_runs=5,
        )
        sla = "PASS" if stats["under_100ms"] else "FAIL"
        print(
            f"{num_tickers:>8} {obs_per_ticker:>10} {stats['total_obs']:>10,} "
            f"{stats['mean_ms']:>10.2f} {stats['p99_ms']:>8.2f} {sla:>8}"
        )

    print("=" * 78)
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Ledger ASOF engine benchmark")
    parser.add_argument("--tickers", type=int, default=None, help="Number of tickers")
    parser.add_argument("--obs", type=int, default=None, help="Observations per ticker")
    parser.add_argument("--bars", type=int, default=1_000, help="Bars per ticker")
    parser.add_argument("--runs", type=int, default=5, help="Timed benchmark runs")
    parser.add_argument("--sweep", action="store_true", help="Run multi-scale sweep")
    args = parser.parse_args()

    if args.sweep or (args.tickers is None and args.obs is None):
        run_scale_sweep()
    else:
        num_tickers = args.tickers or 10
        obs_per_ticker = args.obs or 100
        stats = run_benchmark(
            num_tickers=num_tickers,
            obs_per_ticker=obs_per_ticker,
            num_bars=args.bars,
            timed_runs=args.runs,
        )
        print_results(stats)


if __name__ == "__main__":
    main()
