# ADR-005: Corporate Action Timing and Known-From Resolution Convention

* **Status:** Accepted
* **Date:** 2026-09-12
* **Authors:** Ledger Quantitative Data Engineering Team
* **Deciders:** Core Architecture Team
* **Technical Domain:** Ingestion & Corporate Actions Adjustment Engine

---

## 1. Context & Problem Statement

Corporate actions (stock splits, reverse splits, cash dividends, and stock dividends) fundamentally alter asset pricing and historical returns. In financial theory, corporate actions involve three distinct dates:
1. **`announcement_date`:** When the corporate board of directors publicly announces the action (e.g. Apple announcing a 4-for-1 stock split on 2020-07-30).
2. **`ex_date`:** The effective market trading session where the stock begins trading on an adjusted basis (e.g. Apple trading split-adjusted on 2020-08-31).
3. **`known_from` (Transaction Time):** The exact point in time when this event is officially known and actionable to the feature store engine.

### The Vendor Feed Limitation
Public historical data feeds (e.g., `yfinance`, Stooq) provide split and dividend series indexed strictly by `ex_date`, omitting formal SEC press release timestamps or regulatory announcement times.

If an ingestion pipeline naive sets `known_from` to the beginning of the `ex_date` or uses a pre-adjusted series:
- A strategy evaluating signals on the ex-date morning might mistakenly apply adjustments before closing price confirmation.
- Strategies evaluating signals in the pre-announcement window might suffer severe look-ahead bias if pre-adjusted prices are used.

We must define a deterministic, conservative convention for corporate action timestamps.

---

## 2. Decision

> **For corporate actions ingested from public daily feeds without SEC filing timestamps, we mandate: `announcement_date = ex_date` and `known_from = ex_date session close + 15 min buffer` (16:15 ET converted to UTC).**

### Key Invariants

1. **Conservative No-Leakage Guarantee:**
   * A stock split with `ex_date = 2020-08-31` receives `known_from = 2020-08-31T20:15:00Z` (16:15 EDT).
   * Backtesting queries evaluated *prior* to `2020-08-31T20:15:00Z` (e.g. July 2020 or August 2020 pre-market) see strictly unadjusted prices with Cumulative Adjustment Factor $\text{CAF} = 1.0$.
   * Backtesting queries evaluated *after* `2020-08-31T20:15:00Z` dynamically compute $\text{CAF} = 4.0$ across historical prices.

2. **Decoupled Physical Storage:**
   * Price bars (`fact_market_ohlcv_raw`) and corporate actions (`fact_corporate_actions`) are stored in separate, immutable Parquet partitions.
   * Prices are **never** mutated or pre-scaled on disk.

3. **Future SEC EDGAR Extension (Weeks 6–7):**
   * When SEC EDGAR 8-K filings are ingested, `announcement_date` and `known_from` will be upgraded to the exact `acceptance_time` from the SEC filing feed. The append-only design allows appending new versioned corporate actions without rewriting raw history.

---

## 3. Consequences

### Positive (Gains & Guarantees)
* **Zero Accidental Pre-Scaling:** Completely prevents the #1 backtest defect: retroactively scaling July prices before the August split takes effect.
* **Deterministic Point-in-Time Adjustment:** CAF calculations are strictly pure mathematical functions of observation timestamp $T_{obs}$.

### Negative & Trade-offs (Liabilities & Mitigations)
* **Conservative Announcement Assumption:** In reality, splits are announced 2–4 weeks before their ex-date. Using `announcement_date = ex_date` means strategies cannot trade on the *announcement* news itself until ex-date.
  * *Mitigation:* This conservative rule guarantees zero look-ahead leakage. The SEC EDGAR extension will provide true announcement timestamps.

---

## 4. Alternatives Considered & Rejected

### Option A: Using Vendor Pre-Adjusted Prices (Yahoo Finance `Adj Close`)
* **Description:** Store pre-adjusted prices directly.
* **Why Rejected:** Introduces massive look-ahead bias across all pre-split historical intervals, invalidating backtests.

### Option B: Setting `known_from` to Midnight of Ex-Date (`00:00:00`)
* **Description:** Assume splits are known at midnight before market open.
* **Why Rejected:** Violates exchange settlement hours and creates race conditions with pre-market orders.

---

## 5. References & Prior Art
* Center for Research in Security Prices (CRSP) Corporate Action Distribution Schema.
* Ledger Leakage Canary Suite: `tests/canaries/test_canary_02_retroactive_splits.py`.
* ADR-001: Append-Only Immutable Storage with Derived Upper Bounds.
