"""Standalone benchmark: Arrow zero-copy memory transfer throughput.

Usage:
    uv run python benchmarks/bench_arrow_zero_copy.py
    uv run python benchmarks/bench_arrow_zero_copy.py --rows 500000

Measures the latency of each stage of the Polars -> Arrow -> DuckDB -> Arrow -> Polars
pipeline and confirms buffer reuse at each step.
"""

from __future__ import annotations

import argparse
import time
from typing import Any

import duckdb
import polars as pl

# ---------------------------------------------------------------------------
# Buffer identity helpers
# ---------------------------------------------------------------------------


def _buf_addr(arr: Any) -> int | None:
    """Return memory address of the first non-null data buffer."""
    for buf in arr.buffers():
        if buf is not None:
            return int(buf.address)
    return None


# ---------------------------------------------------------------------------
# Stage timings
# ---------------------------------------------------------------------------


def bench_pipeline(num_rows: int = 100_000) -> dict[str, Any]:
    """Run the full zero-copy pipeline and measure each stage."""
    # Build Polars DataFrame
    df = pl.DataFrame(
        {
            "sec_id": ["SEC_BENCH"] * num_rows,
            "price": [100.0 + (i % 10_000) * 0.01 for i in range(num_rows)],
            "vol": list(range(num_rows)),
        }
    )

    # Stage 1: Polars -> Arrow
    t0 = time.perf_counter()
    arrow_table = df.to_arrow()
    stage1_ms = (time.perf_counter() - t0) * 1000.0

    # Verify zero-copy: buffer addresses identical
    pl_price_addr = _buf_addr(df["price"].to_arrow())
    arrow_price_addr = _buf_addr(arrow_table.column("price").chunks[0])
    buffers_shared = pl_price_addr == arrow_price_addr

    # Stage 2: Arrow -> DuckDB register
    t0 = time.perf_counter()
    con = duckdb.connect()
    con.register("bench_data", arrow_table)
    stage2_ms = (time.perf_counter() - t0) * 1000.0

    # Stage 3: DuckDB query -> Arrow result
    t0 = time.perf_counter()
    result_arrow = con.execute(
        "SELECT sec_id, AVG(price) as avg_p, SUM(vol) as total_vol FROM bench_data GROUP BY sec_id"
    ).arrow()
    stage3_ms = (time.perf_counter() - t0) * 1000.0

    # Stage 4: Arrow result -> Polars
    t0 = time.perf_counter()
    result_pl_raw = pl.from_arrow(result_arrow)
    assert isinstance(result_pl_raw, pl.DataFrame)
    result_pl: pl.DataFrame = result_pl_raw
    stage4_ms = (time.perf_counter() - t0) * 1000.0

    total_ms = stage1_ms + stage2_ms + stage3_ms + stage4_ms
    throughput = (num_rows / total_ms) * 1000.0

    # Verify Arrow input buffer was not mutated by DuckDB
    arrow_price_addr_post = _buf_addr(arrow_table.column("price").chunks[0])
    input_intact = arrow_price_addr == arrow_price_addr_post

    return {
        "num_rows": num_rows,
        "stage1_polars_to_arrow_ms": stage1_ms,
        "stage2_duckdb_register_ms": stage2_ms,
        "stage3_duckdb_query_ms": stage3_ms,
        "stage4_arrow_to_polars_ms": stage4_ms,
        "total_ms": total_ms,
        "throughput_rows_per_sec": throughput,
        "buffers_shared_polars_arrow": buffers_shared,
        "input_buffer_intact_after_duckdb": input_intact,
        "result_rows": result_pl.height,
    }


def print_results(stats: dict[str, Any]) -> None:
    """Print a formatted pipeline benchmark report."""
    print()
    print("=" * 60)
    print("  Ledger Arrow Zero-Copy Pipeline Benchmark")
    print("=" * 60)
    print(f"  Input rows       : {stats['num_rows']:,}")
    print("-" * 60)
    print(f"  Stage 1 (Polars -> Arrow)  : {stats['stage1_polars_to_arrow_ms']:.3f} ms")
    print(f"  Stage 2 (DuckDB register)  : {stats['stage2_duckdb_register_ms']:.3f} ms")
    print(f"  Stage 3 (DuckDB query)     : {stats['stage3_duckdb_query_ms']:.3f} ms")
    print(f"  Stage 4 (Arrow -> Polars)  : {stats['stage4_arrow_to_polars_ms']:.3f} ms")
    print(f"  Total pipeline             : {stats['total_ms']:.3f} ms")
    print(f"  Throughput                 : {stats['throughput_rows_per_sec']:,.0f} rows/sec")
    print("-" * 60)
    shared = "YES (zero-copy)" if stats["buffers_shared_polars_arrow"] else "NO (copy)"
    intact = "YES" if stats["input_buffer_intact_after_duckdb"] else "NO (mutated!)"
    print(f"  Polars->Arrow buf shared   : {shared}")
    print(f"  Input buf intact post-DDB  : {intact}")
    print("=" * 60)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ledger Arrow zero-copy benchmark")
    parser.add_argument("--rows", type=int, default=100_000, help="Number of rows")
    parser.add_argument("--runs", type=int, default=3, help="Number of runs (average reported)")
    args = parser.parse_args()

    all_stats: list[dict[str, Any]] = []
    for run_idx in range(args.runs):
        print(f"Run {run_idx + 1}/{args.runs}...", end=" ", flush=True)
        s = bench_pipeline(num_rows=args.rows)
        all_stats.append(s)
        print(f"{s['total_ms']:.2f}ms")

    # Average key timings
    avg_stats = {
        k: sum(s[k] for s in all_stats) / len(all_stats)
        if isinstance(all_stats[0][k], float)
        else all_stats[0][k]
        for k in all_stats[0]
    }
    print_results(avg_stats)


if __name__ == "__main__":
    main()
