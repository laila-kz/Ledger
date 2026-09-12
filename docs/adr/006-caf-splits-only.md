# ADR-006: CAF Adjusts for Splits Only, Not Dividends

* **Status:** Accepted
* **Date:** 2026-09-12
* **Authors:** Ledger Quantitative Data Engineering Team
* **Deciders:** Core Architecture Team
* **Technical Domain:** ASOF Engine / Cumulative Adjustment Factor

---

## 1. Context & Problem Statement

When computing the Cumulative Adjustment Factor (CAF) for historical prices (see ADR-005),
two distinct corporate event types require price adjustment:

1. **Stock splits** (e.g. AAPL 4:1 on 2020-08-31): The share count multiplies and the
   price divides by the same factor on the ex_date. A pre-split price of \ becomes
   comparable to a post-split price of \ only if multiplied by 4.0.

2. **Cash dividends** (e.g. AAPL quarterly dividend of \.22/share): On the ex_date,
   the share price drops by approximately the dividend amount as the liability transfers
   to shareholders. "Dividend-adjusted" prices subtract this drop so that a continuous
   return series has no artificial negative gap on the ex_date.

Both yfinance's uto_adjust=True and most commercial data vendors bundle both
adjustments into a single "Adj Close" column by default.

The question for Week 2 is: **should the CAF engine apply both adjustments dynamically,
or splits only?**

This ADR records the decision and the reasoning.

---

## 2. Decision

> **We will implement CAF as a splits-only adjustment factor. Cash dividends will NOT be
> applied to raw close prices in the CAF engine. The stored close column will be
> split-adjusted but dividend-unadjusted (equivalent to yfinance Close, not Adj Close).**

### Key Architectural Invariants

1. **CAF formula scope:** The product runs only over rows where ction_type = 'SPLIT'
   in act_corporate_actions. Dividend rows (ction_type = 'CASH_DIVIDEND') are
   excluded from the CAF computation.

2. **Stored price semantics:** The close column in act_market_ohlcv_raw is the
   split-adjusted close provided by yfinance (Close, not Adj Close). This means
   split continuity is already handled at ingestion time; the CAF engine exists to
   restore split-adjusted comparability across different observation timestamps — not
   to further adjust for dividends.

3. **Dividend data retained separately:** Cash dividend records are stored in
   act_corporate_actions with ction_type = 'CASH_DIVIDEND' and a cash_amount
   column. They are available for total return calculations in future work.

4. **Scope boundary:** All features in ledger/features/definitions/technical.py (Week 2)
   operate on dj_close computed by the CAF engine, which means they use price returns
   **excluding dividend income**. This is explicitly a price return, not a total return.

---

## 3. Consequences

### Positive (Gains & Guarantees)

* **Bitemporal safety is fully preserved for splits.** The dangerous leakage vector
  (backtesting across a split date with a static adjustment factor) is eliminated by
  the dynamic CAF. This is the highest-risk correctness issue in quant backtesting.

* **No continuous-compounding complexity.** Dividend adjustment requires accumulating
  a compound factor over hundreds of quarterly payments, which compounds floating-point
  precision errors and creates subtle look-ahead risk when historical dividends are
  restated. Excluding dividends sidesteps this entirely.

* **Simpler implementation, easier to audit.** The CAF formula involves a simple integer
  product of a handful of split ratios per security per decade. A single AAPL example
  (4:1 split on 2020-08-31) is enough to fully specify and test the engine. Dividend
  adjustment would add an additional dimension (reinvestment rate, ex_date discount
  rate) that is out of scope for a momentum / volatility feature store.

* **Industry standard for momentum signals.** Academic momentum factors (Jegadeesh &
  Titman 1993, Fama-French) are defined on price returns, not total returns. Using
  split-adjusted-only prices is consistent with the literature.

### Negative & Trade-offs (Liabilities & Mitigations)

* **Price returns != total returns.** Features built on dj_close will understate
  the true holding-period return of high-dividend-yield stocks (e.g. utilities,
  REITs) relative to growth stocks. A cross-sectional momentum signal trained on
  price returns will have a mild bias against high-dividend-yield securities.
  * *Mitigation:* Week 2 technical features are momentum and volatility signals, which
    are typically defined on price returns in the academic literature. A total return
    variant can be added later as a separate feature definition.

* **Dividend adjustment left as future work.** If a Total Return Index feature is
  required (e.g. for fixed income or multi-asset strategies), the act_corporate_actions
  table already stores cash_amount and ex_date. A future compute_total_return_caf()
  function can extend this engine by accumulating dividend factors alongside split factors.
  * *Mitigation:* The interface for compute_caf_matrix() is parameterised by a
    splits_df argument. Extending it to accept a dividends_df argument requires no
    breaking changes.

---

## 4. Alternatives Considered & Rejected

### Option A: Full Dividend Adjustment (Total Return)

* **Description:** Include cash dividend factors in the CAF product alongside splits.
  The dividend adjustment factor for date t is:
  (price_on_ex_date - cash_amount) / price_on_ex_date
  applied cumulatively for all dividends with ex_date in (t, T_obs].
* **Why Rejected:** (1) Dividend adjustment requires knowledge of the exact ex_date
  closing price to compute the factor — creating a circular dependency between the price
  and its own adjustment factor. (2) Reinvestment assumptions (where does the dividend
  cash go?) are subjective and signal-dependent. (3) Most quant momentum literature
  uses price returns. (4) Substantially increases implementation complexity and
  floating-point precision risk in Week 2 scope.

### Option B: Vendor-Adjusted Prices (Use yfinance Adj Close Directly)

* **Description:** Store Adj Close from yfinance at ingestion time (fully adjusted
  for both splits and dividends as of the ingestion date). Apply no dynamic CAF.
* **Why Rejected:** This is precisely the anti-pattern the Ledger system exists to
  prevent. Adj Close applies all future splits and dividends retroactively at the
  time of download. If AAPL does a second split in 2025, re-downloading data will
  retroactively revise the July 2020 Adj Close, silently corrupting all historical
  backtests without any audit trail. This violates ADR-001 (append-only immutability)
  and ADR-005 (dynamic CAF).

### Option C: Static Adjustment Factors Baked In at Ingestion

* **Description:** At ingestion time, compute the cumulative split factor from all
  known splits as of the ingestion timestamp and store dj_close as a physical column.
* **Why Rejected:** The stored dj_close becomes stale as soon as any future split
  is announced (the factor changes retroactively). Correcting it requires rewriting
  historical Parquet files, violating ADR-001. The dynamic CAF engine exists precisely
  because the correct adjustment factor depends on the observation timestamp, not the
  ingestion timestamp.

---

## 5. Future Work

When a Total Return signal is required:

`python
# Planned extension (not in scope for Week 2)
def compute_total_return_caf(
    prices_df: pl.DataFrame,
    splits_df: pl.DataFrame,
    dividends_df: pl.DataFrame,  # New argument
    observation_df: pl.DataFrame,
) -> pl.DataFrame:
    ...
`

The dividend rows in act_corporate_actions (ction_type = 'CASH_DIVIDEND',
cash_amount column) are already stored and available for this computation.

---

## 6. References & Prior Art

* Jegadeesh, N. & Titman, S. (1993). *Returns to Buying Winners and Selling Losers.*
  Journal of Finance, 48(1), 65-91.
* Fama, E. & French, K. (1996). *Multifactor Explanations of Asset Pricing Anomalies.*
  Journal of Finance, 51(1), 55-84.
* Ledger caf.py implementation: ledger/features/caf.py.
* Ledger ADR-001: Append-Only Immutable Storage.
* Ledger ADR-005: Dynamic CAF Engine (observation-timestamp-gated split adjustment).
