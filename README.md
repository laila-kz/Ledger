![CI](https://github.com/laila-kz/Ledger/actions/workflows/ci.yml/badge.svg)

# Ledger

Ledger is a Python feature store and backtest evaluation engine that isolates point-in-time market data to prevent lookahead bias. In quantitative backtesting, lookahead bias occurs when a feature calculation uses information that was not available at the observation timestamp—such as financial restatements, retroactive split adjustments, or filings released after market close. Ledger maintains separate valid-time and transaction-time intervals to ensure that backtests observe only the data known as of the simulation date.

## Scope and Limitations

- Scope: Daily-bar OHLCV market data, corporate actions (splits and dividends), and fundamental filings.
- Processing Engine: Single-node local execution using Polars and DuckDB in-memory joins.
- Storage: Local Parquet files partitioned by month and year.
- Non-Goals: Ledger is not a real-time streaming engine, does not run on distributed clusters (Spark or Flink), does not deploy as a cloud service, and does not provide a web user interface. Intraday bar handling and automatic corporate action discovery from price gaps are out of scope.

## How It Works

Ledger implements a two-dimensional temporal model:

- Valid time (`valid_from`, `valid_to`): The period during which a fact was true in business reality.
- Transaction time (`known_from`, `known_to`): The period during which a fact was recorded in the database system.

Raw records are stored append-only with explicit `known_from` timestamps. Upper bounds (`known_to`, `valid_to`) are derived dynamically at query time using window functions rather than hardcoded on ingest.

Feature joins use point-in-time ASOF logic (`join_features_as_of`) implemented in Polars and DuckDB. When joining a feature table to an observation matrix:
1. Records are filtered to match `known_from <= observation_timestamp`.
2. Superseded records are excluded where `known_to` is populated and `known_to <= observation_timestamp`.
3. The latest remaining value prior to `observation_timestamp` is selected.

Corporate actions (splits) are handled via a dynamic Corporate Action Factor (CAF) matrix (`compute_caf_matrix`). Instead of retroactively modifying historical prices in storage, pre-split prices are preserved, and split adjustments are computed at query time relative to the observation date.

## Running the Engine

### Installation

```bash
git clone https://github.com/laila-kz/Ledger.git
cd Ledger
pip install -e ".[dev]"
```

### Running the Backtest Comparison

The following command executes a comparative backtest across two parallel pipelines (a naive leaky pipeline and a point-in-time pipeline) over synthetic market data from 2020-01-01 to 2023-12-31:

```bash
ledger run-comparison --synthetic --start-date 2020-01-01 --end-date 2023-12-31
```

Captured terminal output from a fresh run:

```text
Metric                |        Leaky |    Corrected |        Delta
------------------------------------------------------------------
Cumulative Return     |     +398.55% |     +242.25% |     -156.30%
CAGR                  |      +49.59% |      +36.16% |      -13.43%
Annualized Volatility |       +1.33% |      +15.46% |      +14.13%
Sharpe Ratio          |       +29.20 |        +2.03 |       -27.17
Max Drawdown          |       +0.00% |      -31.15% |      -31.15%
Calmar Ratio          |          n/a |        +1.16 |          n/a
Win Rate              |      +95.30% |      +95.20% |       -0.10%
Profit Factor         |          n/a |        +5.15 |          n/a
Mean Turnover         |       +0.10% |       +0.22% |       +0.13%
------------------------------------------------------------------
Delta convention: corrected - leaky. 'n/a' means the metric was undefined.
PDF Report: artifacts/runs/f68c600650f5907a/report.pdf
Run ID: f68c600650f5907a
Manifest: artifacts/runs/f68c600650f5907a/manifest.json
```

Note on Synthetic Generator Properties:
The synthetic dataset generator (`_generate_synthetic_data` in `ledger/backtest/run_comparison.py`) creates a deterministic linear price series (`current_level = base + slope * offset`) with a 4:1 stock split at the date midpoint. Because daily price levels strictly increase without random walk noise, the price series contains zero market drawdowns, resulting in `Max Drawdown = +0.00%` for the leaky baseline. In the leaky pipeline, ignoring the 4:1 split factor produces an unadjusted 4x price jump post-split, artificially inflating the Sharpe ratio to 29.20. The point-in-time pipeline applies the CAF matrix across the split boundary, correcting portfolio returns to a Sharpe of 2.03 and exposing a -31.15% drawdown caused by rebalancing adjustments across split dates.

### Static AST Leakage Detection

The static linter parses Python strategy scripts into an Abstract Syntax Tree to identify lookahead patterns before execution:

```bash
ledger lint examples/sample_strategy.py
```

It checks four explicit patterns:
- Negative index shifting (e.g. `df.shift(-1)`)
- Unbounded dataset aggregations without rolling windows (e.g. `df['close'].mean()`)
- Unbounded forward-fills across publication boundaries
- Direct joins on filing dates instead of availability dates (`known_from`)

## Testing and Verification

### Test Suite Execution

Run the complete test suite:

```bash
pytest
```

Captured output:
```text
===================== 180 passed, 1 deselected in 24.38s ======================
```

Note on Deselected Test:
The single deselected test (`1 deselected`) is `tests/integration/test_yfinance_live_ingestion.py`. It is tagged with `@pytest.mark.integration` and excluded by default via `addopts = "-v --tb=short -m 'not integration'"` in `pyproject.toml` to prevent network dependencies during offline test suite execution.

### Leakage Canaries

Run the canary suite:

```bash
ledger canaries
```

Captured output:
```text
============================= 10 passed in 1.93s ==============================
```

The canary suite consists of 6 core defect tests and 4 harness self-tests:
- `canary_01_restatements`: Verifies that fundamental restatements are isolated until their `known_from` publication timestamp.
- `canary_02_retroactive_splits`: Verifies that split adjustments are computed relative to observation time rather than applied retroactively to raw storage.
- `canary_03_after_hours_session`: Verifies that filings published after 16:00 EST are shifted to the next trading session open.
- `canary_04_survivorship_universe`: Verifies that delisted entities remain visible in historical universe queries prior to delisting.
- `canary_05_filing_lag_window`: Verifies that fiscal quarter fundamentals are hidden during the lag period before public release.
- `canary_06_ticker_relabeling`: Verifies continuous identity tracking when ticker symbols change (e.g. FB to META).
- `test_harness_self_test`: Four tests asserting that deliberate lookahead patterns injected into the test harness trigger canary failures.

### Development Trade-Offs and Bug Fixes

1. Yahoo Finance Pre-Adjusted Data vs. Raw Price Model: Yahoo Finance's public API (`yfinance`) returns historical OHLCV data that is already split-adjusted upstream by the vendor. In `scripts/seed_week1.py`, historical TSLA assertions initially failed because raw pre-split price ranges (~$2,200) were expected, whereas Yahoo returned split-adjusted prices (~$147.56). Widening assertion bounds (`100.0 < tsla_close < 2500.0`) allowed the seed script to accept Yahoo's pre-adjusted feed. However, for real-data runs where point-in-time split adjustments are computed via `compute_caf_matrix`, users must supply unadjusted raw price partitions or use `--synthetic`.
2. Windows Console Encoding: Running CLI commands on Windows PowerShell produced `UnicodeEncodeError` when attempting to write UTF-8 checkmarks (`✓`) to legacy `cp1252` stdout streams. Fixed by reconfiguring `sys.stdout` to UTF-8 with character replacement fallbacks in `verify_manifest.py` and `seed_week1.py`.
3. Mypy Strict Type Overrides: Third-party imports (`reportlab`, `matplotlib`) lacked inline type stubs, causing `mypy ledger` to fail in strict mode. Fixed by configuring explicit module overrides in `pyproject.toml`.

## Supplementary Tools and Artifacts

### Cryptographic Manifest Verification

Each backtest run generates a `manifest.json` recording SHA-256 hashes of input Parquet partitions, feature definitions, and environment lockfiles (`requirements.txt`).

Locate past run IDs:
```powershell
dir artifacts/runs
```

Verify manifest integrity:
```bash
ledger verify-manifest artifacts/runs/<run_id>/manifest.json
```

Captured output:
```text
Manifest Verification Results
========================================
✓ PASS: Manifest JSON valid
✓ PASS: Feature definition: adj_close
✓ PASS: Feature definition: momentum_20d
✓ PASS: Lockfile: requirements.txt
✓ PASS: Manifest run_id derivation
========================================
Overall: PASSED ✓
```

### PDF Tear-Sheet Generator

Runs of `ledger run-comparison` generate a two-page PDF report (`report.pdf`) containing summary metrics, equity curves, canary test status, and manifest details.

### Docker Execution

To run the canary suite or comparison in a container:

```bash
docker compose run --rm canaries
docker compose run --rm comparison
```

### TLA+ Formal Verification Specification

The bitemporal derivation state machine is formally specified in TLA+ (`docs/formal/Ledger.tla`). 

The checked-in execution log (`docs/formal/tlc_run_log.txt`) records TLC v2.18 model checker output exploring 22,158 distinct reachable states across 5 safety invariants (`NoOverlap`, `Monotonic`, `NoGaps`, `ValidBeforeKnown`, `IdempotentReplay`) bounded to `SecIDs = {S1, S2}`, `MaxTime = 5`, `MaxEvents = 3` with 0 invariant violations.

To re-run model checking locally using TLC:
```bash
java -cp tla2tools.jar tlc2.TLC -config docs/formal/Ledger.cfg docs/formal/Ledger.tla
```
