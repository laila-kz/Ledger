# Week 4 Guide: Comparative Backtest Harness & Hash-Verified Lineage

**Primary Objective:** Build a clean, vectorized quant backtest consumer that runs a reference trading strategy across two parallel pipelines (Leaky vs. Corrected), generate comparative performance metrics (Sharpe, Drawdown, Alpha decay), and implement the hash-verified reproducibility manifest.

---

## 1. Definition of Done (DoD) for Week 4
- [ ] Reference quantitative strategy implemented (`ledger/backtest/strategy.py`).
- [ ] Leaky vs. PIT execution runner built (`ledger/backtest/runner.py`, `ledger/backtest/run_comparison.py`).
- [ ] Comparative tear-sheet and metrics generator operational (`ledger/backtest/tear_sheet.py`).
- [ ] Hash-verified reproducibility manifest generator implemented with SHA-256 data, code, and lockfile hashing (`ledger/lineage/manifest.py`).
- [ ] CLI execution command `python -m ledger.backtest.run_comparison` produces complete comparative terminal output and JSON manifest.

---

## 2. Day-by-Day Implementation Plan

### Day 1: Reference Quantitative Strategy
1. **Design a Simple, Transparent Strategy:**
   - **Cross-Sectional Momentum + Volatility Scaling:**
     - Universe: Top 10–20 liquid US tech/growth stocks over a 3–5 year historical window (including a split event like AAPL/TSLA 2020).
     - Signal: Rank assets by 20-day Momentum (`momentum_20d`), filter by trend (`sma_50d`), and inverse-volatility weight positions (`volatility_20d`).
     - Rebalance Frequency: Weekly or Daily EOD.
2. **Implement `ledger/backtest/strategy.py`:**
   - Write pure vectorized NumPy/Polars signal generation consuming a feature DataFrame and producing target portfolio weights.

### Day 2: The Two-Pipeline Runner
1. **Implement `ledger/backtest/runner.py`:**
   - **Pipeline A (Leaky Control Pipeline):**
     - Ingests pre-adjusted Yahoo Finance prices.
     - Ignores exchange calendar cutoffs (same-day after-hours earnings availability).
     - Static survivor-biased universe.
   - **Pipeline B (Ledger Corrected Pipeline):**
     - Consumes features strictly generated via `join_features_as_of()`.
     - Dynamically calculated CAF adjustments.
     - Strict exchange calendar cutoffs.
2. **Vectorized Simulation:**
   - Compute daily portfolio returns, transaction costs (e.g., 5 bps slippage/commission), and cumulative equity curves for both pipelines.

### Day 3: Comparative Tear-Sheet & Alpha Decay Analytics
1. **Implement `ledger/backtest/tear_sheet.py`:**
   - Compute key performance metrics for both pipelines:
     - Cumulative Return & Annualized CAGR
     - Annualized Sharpe Ratio ($R_f = 0\%$)
     - Maximum Drawdown (MDD) & Calmar Ratio
     - Daily Win Rate & Profit Factor
   - **Leakage Attribution Summary:**
     - Highlight the exact performance spread ($\Delta \text{Sharpe}$, $\Delta \text{Return}$) caused by look-ahead bias.

### Day 4: Hash-Verified Lineage & Run Manifest
1. **Implement `ledger/lineage/manifest.py`:**
   - Calculate SHA-256 hashes of:
     - All input raw Parquet datasets (`fact_market_ohlcv_raw.parquet`, `fact_corporate_actions.parquet`).
     - Declarative feature definition files.
     - Project lockfile (`poetry.lock` / `uv.lock` / `requirements.txt`).
   - Capture environment metadata:
     - `ledger_version`, `git_commit_sha`, `python_version`, UTC execution timestamp.
   - Write deterministic `manifest.json` per backtest run in `artifacts/runs/<run_id>/manifest.json`.

### Day 5: CLI Runner & End-to-End Integration Test
1. **Implement `ledger/backtest/run_comparison.py`:**
   - Build CLI entrypoint accepting `--start-date`, `--end-date`, and `--tickers`.
   - Execute both backtests, print comparative ASCII tear-sheet to stdout, and save `manifest.json` and equity curves.
2. **Write Integration Test `tests/unit/test_backtest_integration.py`:**
   - Assert that running the comparison produces expected metric deltas and valid manifest hashes.

---

## 3. Key Architectural Traps to Avoid in Week 4
- **Trap 1:** Building an overly complex, event-driven trading engine. The purpose of this project is to prove **feature input correctness**, not build a generic OMS/EMS. Keep the backtest vectorized, transparent, and fast.
- **Trap 2:** Hiding the leakage effect. Ensure the reference strategy touches at least one split date (e.g. August 2020) and restatement so the delta between the leaky and corrected pipeline is clearly visible.
- **Trap 3:** Non-deterministic manifests. Ensure JSON keys and dictionary hashes are sorted alphabetically so the manifest output is 100% reproducible byte-for-byte.
