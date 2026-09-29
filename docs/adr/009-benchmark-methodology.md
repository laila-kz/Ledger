# ADR 009: Benchmark Methodology — ASOF Engine & Arrow Zero-Copy

## Status
Accepted — recorded 2026-09-13 (Week 2 Day 5)

## Context
Ledger commits to two quantitative performance guarantees documented in
[`guides/week-02-asof-engine-and-dynamic-caf.md`](../../guides/week-02-asof-engine-and-dynamic-caf.md):

1. **ASOF Join SLA**: The vectorized `join_features_as_of` engine must complete
   in **< 100 ms** for a realistic observation matrix of 1,000+ rows across
   multiple securities.
2. **Zero-Copy Memory Exchange**: Data must move between Polars and DuckDB via
   the Apache Arrow C Data Interface **without byte-level copying**. No
   intermediate CSV or Parquet files are written between engines.

These guarantees matter because:
- A 100ms per 1,000 observations budget translates to ≤ 25 seconds for a full
  500-ticker backtest with 500 rebalances — acceptable iteration speed.
- Zero-copy eliminates the 10× throughput penalty and stale-copy risk that
  serialization-based engine hand-offs carry.

Benchmarks must be trustworthy. This ADR documents exactly how we measure, so
future contributors cannot accidentally cheat.

---

## Decision

### D1 — Warm-Path Measurement Only

**Rule**: The first 2 function calls ("warm-up runs") are excluded from all
timing measurements.

**Rationale**: The cold path includes Python import machinery, Polars Rust JIT
compilation, CPU instruction cache fill, and OS memory-map warm-up. These costs
amortize to zero across a backtest that calls `join_features_as_of` thousands
of times. Measuring the cold path gives a pessimistic and misleading SLA.

```python
# Correct approach — warm-up excluded from measurement
for _ in range(2):  # warm-up
    join_features_as_of(entity_df, [feature_df])

elapsed_runs = []
for _ in range(5):  # timed
    t0 = time.perf_counter()
    join_features_as_of(entity_df, [feature_df])
    elapsed_runs.append((time.perf_counter() - t0) * 1000)

mean_ms = sum(elapsed_runs) / len(elapsed_runs)
```

### D2 — Realistic Dataset Shape

**Rule**: The benchmark must use **multiple securities** (at least 5), not a
single ticker with 1,000 observations.

**Rationale**: A single-ticker benchmark has one `by="sec_id"` group — Polars
effectively sorts a single array. The realistic workload is 10–100 tickers with
10–200 observations each. This exercises the group-dispatch path, which is
what runs in production.

**Reference shape used**: 10 tickers × 100 obs/ticker = 1,000 observations,
joined against 10 × 1,000 = 10,000 feature rows (varies by scale level).

### D3 — Zero-Copy Verified at Buffer Address Level

**Rule**: Zero-copy is verified by comparing the raw memory buffer address of a
numeric column (via `pyarrow.Array.buffers()[1].address`) **before and after**
the Polars ↔ Arrow boundary crossing.

**Rationale**: Simply checking "no CSV was written" is a weak test. The correct
test is that the underlying allocation is shared, not duplicated.

```python
addr_polars = df["price"].to_arrow().buffers()[1].address
addr_arrow = df.to_arrow().column("price").chunks[0].buffers()[1].address
assert addr_polars == addr_arrow  # zero-copy confirmed
```

Two properties are verified:
1. `Polars.to_arrow()` shares the buffer (zero-copy export).
2. The input Arrow buffer's address is **unchanged** after DuckDB registers and
   queries it (DuckDB reads without copying or mutating the source).

### D4 — SLA Metric is Mean, Not Best-of-N

**Rule**: The SLA assertion `mean_ms < 100.0` uses the **arithmetic mean** of
timed runs, not the minimum or best-of-N.

**Rationale**: Minimum is cherry-picked and misleading. Mean represents average
production throughput. P99 is also reported for visibility into tail latency.

### D5 — pytest vs Standalone Benchmark Separation

**Rule**:
- `tests/unit/test_asof_engine.py` and `tests/unit/test_arrow_zero_copy.py`
  contain **correctness tests and SLA guards**. These run in CI on every push.
- `benchmarks/bench_asof_engine.py` and `benchmarks/bench_arrow_zero_copy.py`
  are **standalone scripts** for developer profiling. They are not collected by
  pytest (no `test_` prefix).

**Rationale**: pytest's per-test process isolation adds overhead that inflates
timing measurements. Standalone scripts measure net algorithm latency.

---

## Consequences

- **CI budget**: The `< 100ms` SLA test (`test_benchmark_1000_observations_sub_100ms`)
  runs on every CI push. If it fails on CI (which runs slower hardware), the
  threshold must be revisited via a new ADR rather than silently raised.
- **Future baselines**: When Week 3 adds the fundamental feature views (P/E, EPS),
  the ASOF join benchmark must be re-run and results updated in
  `docs/CLAIMS.md` §7, which is where current benchmark numbers are recorded.
  (`WAR_LOG.md` was the original target for this; it is now archived under
  `docs/history/` and is not a statement of current state.)
- **Arrow version pinning**: Zero-copy buffer sharing relies on Polars and
  PyArrow using the same Arrow ABI. `pyproject.toml` pins `pyarrow>=17.0` to
  ensure compatibility. If Polars drops direct PyArrow buffer sharing in a
  future release, this ADR must be revisited.

---

## Verification Commands

```bash
# Run the CI benchmark test
uv run pytest tests/unit/test_asof_engine.py::TestVectorizedEnginePerformance -v

# Run the zero-copy test suite
uv run pytest tests/unit/test_arrow_zero_copy.py -v

# Run the standalone scale sweep (developer profiling)
uv run python benchmarks/bench_asof_engine.py --sweep

# Run the Arrow pipeline benchmark
uv run python benchmarks/bench_arrow_zero_copy.py --rows 100000 --runs 3
```
