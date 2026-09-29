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
Cumulative Return     |      +46.64% |      +30.70% |      -15.95%
CAGR                  |      +10.07% |       +6.94% |       -3.13%
Annualized Volatility |      +20.25% |      +20.09% |       -0.17%
Sharpe Ratio          |        +0.56 |        +0.42 |       -0.14
Max Drawdown          |      -28.29% |      -28.29% |       -0.00%
Calmar Ratio          |        +0.36 |        +0.25 |       -0.11
Win Rate              |      +46.07% |      +45.97% |       -0.10%
Profit Factor         |        +1.10 |        +1.07 |       -0.03
Mean Turnover         |      +26.39% |      +27.03% |       +0.65%
------------------------------------------------------------------
Delta convention: corrected - leaky.
Metrics: artifacts/runs/638e1937b1d9db50/metrics.json
PDF Report: artifacts/runs/638e1937b1d9db50/report.pdf
Run ID: 638e1937b1d9db50
Manifest: artifacts/runs/638e1937b1d9db50/manifest.json
```

Note on Synthetic Generator Properties:
The synthetic dataset generator (`generate_synthetic_data` in `ledger/backtest/synthetic.py`) builds a deterministic, seeded geometric-Brownian-motion price path (Itô-corrected drift) with a single 4:1 forward split at the date midpoint, emitted three ways: true as-traded OHLCV, a vendor pre-adjusted close series that already reflects every future split, and the split record. The leaky pipeline reads the pre-adjusted close while ignoring the split factor, so it double-counts the split: the unadjusted 4x level jump inflates cumulative return (+46.64% vs +30.70%) and the Sharpe ratio (+0.56 vs +0.42) against a realistic, non-zero market drawdown. The point-in-time pipeline instead reconstructs as-traded levels on ingest and applies the CAF matrix across the split boundary, so both arms observe the same economic price path and the only difference is the leak. Because the generator is seeded, these figures reproduce on every run; the run ID varies per invocation.

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
===================== 215 passed, 1 deselected in 25.99s ======================
```

Note on Deselected Test:
The single deselected test (`1 deselected`) is `tests/integration/test_ingestion_smoke.py`. It is tagged with `@pytest.mark.integration` and excluded by default via `addopts = "-v --tb=short -m 'not integration'"` in `pyproject.toml` to prevent network dependencies during offline test suite execution. Run it explicitly with `pytest tests/integration -m integration`.

### Leakage Canaries

Run the canary suite:

```bash
ledger canaries
```

Captured output:
```text
============================= 16 passed in 2.49s ==============================
```

The canary suite consists of 16 tests across 8 files:
- `canary_01_restatements`: Verifies that fundamental restatements are isolated until their `known_from` publication timestamp.
- `canary_02_retroactive_splits`: Verifies that split adjustments are computed relative to observation time rather than applied retroactively to raw storage.
- `canary_03_after_hours_session`: Two tests verifying that filings published after 16:00 ET are shifted to the next session open, and that a Friday rebalance cannot consume an after-hours filing.
- `canary_04_survivorship_universe`: Verifies that delisted entities remain visible in historical universe queries prior to delisting.
- `canary_05_filing_lag_window`: Verifies that fiscal quarter fundamentals are hidden during the lag period before public release.
- `canary_06_ticker_relabeling`: Verifies continuous identity tracking when ticker symbols change (e.g. FB to META).
- `canary_07_synthetic_demo_sanity`: Six tests over the synthetic demo asserting that the leaky arm out-returns and out-Sharpes the corrected arm, that removing the injected leak makes the two arms identical, and that the reported Sharpe stays within a plausible range.
- `test_harness_self_test`: Three tests asserting that deliberate lookahead patterns injected into the test harness trigger canary failures.

### Development Trade-Offs and Bug Fixes

1. Vendor Pre-Adjusted Feeds vs. the Raw Price Contract: Yahoo Finance's public API (`yfinance`) returns OHLCV that is already split-adjusted across its *entire* history, so a bar dated before a later split arrives pre-scaled by that split. Stored under a table named "raw", that is look-ahead: the 2022-08-25 TSLA 3:1 split would be visible in a 2020-08-28 price. Ingestion now inverts the vendor adjustment: `_undo_full_history_split_adjustment` scales each bar by the product of split ratios whose ex-date is strictly after that bar's trade date, using the full split history (including splits after the requested window). Volume is left untouched because it is reported as-traded. Verified against ground truth: AAPL 2020-01-02 is stored at $300.35 (75.0875 × 4) and 2020-08-28 at $499.23, so a 2020 level no longer reflects the later 2020-08-31 or 2022-08-25 splits, and real-data runs feed the CAF matrix true as-traded levels.
2. Windows Console Encoding: Running CLI commands on Windows PowerShell produced `UnicodeEncodeError` when attempting to write UTF-8 checkmarks (`✓`) to legacy `cp1252` stdout streams. Fixed by reconfiguring `sys.stdout` to UTF-8 with character replacement fallbacks in `verify_manifest.py` and `seed_week1.py`.
3. Mypy Strict Type Overrides: Third-party imports (`reportlab`, `matplotlib`) lacked inline type stubs, causing `mypy ledger` to fail in strict mode. Fixed by configuring explicit module overrides in `pyproject.toml`.

## Supplementary Tools and Artifacts

### Cryptographic Manifest Verification

Each backtest run generates a `manifest.json` recording SHA-256 hashes of input Parquet partitions, feature definitions, the environment lockfile (`requirements.txt`), and the Git commit SHA, alongside a machine-readable `metrics.json` of the comparison results.

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
✓ PASS: Input dataset: synthetic://deterministic-market-generator
✓ PASS: Feature definition: adj_close
✓ PASS: Feature definition: momentum_20d
✓ PASS: Feature definition: sma_50d
✓ PASS: Feature definition: volatility_20d
✓ PASS: Lockfile: requirements.txt
✓ PASS: Git commit SHA
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
