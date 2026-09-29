"""Measure ASOF join latency at several scales before setting a budget.

Run directly:
    python benchmarks/scale_probe.py

Prints median wall-clock for each (tickers, observations) point, so the
thresholds in the test suite are derived from measurement rather than guessed.
"""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timedelta, timezone

import polars as pl

from ledger.features.engine import join_features_as_of

UTC = timezone.utc
BASE = datetime(2022, 1, 1, 21, 0, tzinfo=UTC)


def build(
    num_tickers: int, obs_per_ticker: int, bars_per_ticker: int
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Return an (observation, feature) pair at the requested scale."""
    sec_ids = [f"SEC_{i:03d}" for i in range(num_tickers)]

    feature_rows: list[dict[str, object]] = []
    for sec in sec_ids:
        for day in range(bars_per_ticker):
            feature_rows.append(
                {
                    "sec_id": sec,
                    "close": 100.0 + day * 0.1,
                    "known_from": BASE + timedelta(days=day),
                }
            )

    obs_rows: list[dict[str, object]] = []
    for sec in sec_ids:
        for i in range(obs_per_ticker):
            obs_rows.append(
                {
                    "sec_id": sec,
                    "observation_timestamp": BASE + timedelta(days=i * 3, hours=10),
                }
            )
    return pl.DataFrame(obs_rows), pl.DataFrame(feature_rows)


def timeit(entity: pl.DataFrame, feature: pl.DataFrame, runs: int) -> float:
    """Return the median wall-clock of `runs` ASOF joins, in milliseconds."""
    samples: list[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        join_features_as_of(entity, [feature])
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def main() -> None:
    print(f"{'tickers':>8}{'obs/ticker':>12}{'total obs':>11}{'bars':>9}{'median ms':>11}")
    for num_tickers, obs_per_ticker, bars in [
        (5, 300, 1000),
        (5, 1200, 1000),
        (10, 1200, 2000),
        (20, 1500, 2000),
    ]:
        entity, feature = build(num_tickers, obs_per_ticker, bars)
        assert entity.height == num_tickers * obs_per_ticker
        median = timeit(entity, feature, runs=5)
        total = num_tickers * obs_per_ticker
        print(f"{num_tickers:>8}{obs_per_ticker:>12}{total:>11}{bars:>9}{median:>11.2f}")


if __name__ == "__main__":
    main()
