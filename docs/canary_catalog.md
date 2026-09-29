---
title: Leakage Canary Catalog
description: Deterministic tests for six classes of look-ahead bias
---

# Leakage Canary Catalog

Ledger's canaries pair a **point-in-time (PIT) result** with a **deliberately leaky reference pipeline**. A canary passes only when the PIT result matches ground truth and the naive result diverges.

Each canary tests a specific class of look-ahead bias that can silently corrupt a backtest.

---

## Canary 01: Restated Fundamentals

**Defect Class:** Data Restatement Leakage

### The Problem

Fundamental data (earnings, revenue, balance sheet metrics) is frequently restated. Most backtesting systems use the "latest" version for all historical dates, creating look-ahead bias.

```
Timeline: Restated Fundamentals

2023-08-01 (Original Filing)  2023-11-15 (Restatement)
     │                              │
     ▼                              ▼
   EPS = $1.00               EPS = $0.70 (amended)
   
Naive approach: Use $0.70 for EVERY observation up to 2023-08-01
                (Answer: How did we know the restatement?)
                
PIT approach:   Use $1.00 until 2023-11-15 17:30 UTC (known_from)
                Then use $0.70 thereafter
                (Answer: We can only use what we knew at the time)
```

### Mathematical Definition

Define `fundamentals(t, known_at)` as the value of a metric at observation time `t`, queried at time `known_at`.

**Leaky behavior:**
```
leaky_eps(t) = latest_eps_value  ∀ t ≤ known_from(latest_eps)
```

**Correct behavior (PIT):**
```
pit_eps(t, known_at) = eps_version(max(v | known_from(v) ≤ known_at))
```

### Test Assertion

**Scenario:** Q2 2023 EPS filed as `$1.00` on 2023-08-01 09:30 UTC, restated to `$0.70` on 2023-11-15 17:30 UTC.

**Canary 01 Validates:**
- Observation on 2023-08-01 (10:00 UTC) returns `$1.00` ✓
- Observation on 2023-11-15 (16:00 UTC) returns `$1.00` ✓ (restatement not yet known)
- Observation on 2023-11-15 (18:00 UTC) returns `$0.70` ✓ (restatement is now known)

---

## Canary 02: Retroactive Split Adjustment

**Defect Class:** Corporate Action Leakage

### The Problem

Stock splits are applied retroactively. A naive system applies all splits to the complete price history, causing future splits to artificially lower historical nominal price levels before the corporate action occurs.

```
Timeline: Retroactive Split Adjustment

July 2020: Price = $400        Aug 31, 2020: 4:1 Split Announced & Executed
   │                                    │
   ▼                                    ▼
                          
Naive approach: $400 / 4 = $100 for July (WRONG: future 4:1 split leaked retroactively)
                
Ledger backward-adjustment convention:
   Before split known: CAF = 1.0   → Price = $400
   After split known:  CAF = 0.25  → Price = $400 × 0.25 = $100
                       (we store original $400, apply CAF dynamically at query time)
```

### Test Assertion

**Scenario:** July 2020 unadjusted price = $400, 4:1 split on 2020-08-31 with known_from = 2020-09-01.

**Canary 02 Validates:**
- July observation (before split known): `CAF = 1.0`, adjusted_price = `$400` ✓
- August observation (before split known): `CAF = 1.0`, adjusted_price = `$400` ✓
- September observation (after split known): `CAF = 0.25`, adjusted_price = `$100` ✓

---

## Canary 03: After-Hours Session

**Defect Class:** Session Boundary Leakage

### The Problem

Market filings (8-K, 10-Q, 10-K) often arrive after market close. A naive system uses the filing timestamp directly, allowing observations during market close to consume data from the future trading session.

```
Timeline: After-Hours Filing Session Shift

Fri 2023-04-14 17:00 EDT (after close)
         │
         ▼ Naive: Use 2023-04-14 17:00 as observation time (WRONG)
                  Friday's close can see Friday evening's filing
                  
         │
         ▼ PIT Correct: Shift to Mon 2023-04-17 09:30 EDT
                        Next session open. Now Friday's trading
                        cannot see a future filing.
```

### Test Assertion

**Scenario:** Friday 2023-04-14 17:00 EDT filing on Facebook (ticker FB).

**Canary 03 Validates:**
- Rebalance on Friday 15:59 EDT cannot see filing (not yet published) ✓
- Rebalance on Monday 09:30 EDT sees filing (published Friday, actionable Monday) ✓
- Rebalance on Friday 17:00 EDT not possible (market closed) ✓

---

## Canary 04: Survivorship Universe

**Defect Class:** Universe Composition Leakage

### The Problem

Most backtesting systems build a historical universe from current constituents. This introduces survivorship bias—delisted companies disappear from the past, and companies that didn't exist yet appear in the beginning.

```
Timeline: Survivorship Universe Bias

2008-09-15: Lehman Bros Delisted
       │
       ▼
       
Naive: Use current S&P 500 constituents (Lehman removed)
       → Lehman excluded from all historical dates
       → No short-selling opportunity in 2008 backtest
       
PIT: Track valid_from / valid_to for each entity
     Before 2008-09-15: "LEH" is member = TRUE
     After  2008-09-15: "LEH" is member = FALSE (or ticker changes to "LEHMQ")
```

### Test Assertion

**Scenario:** Lehman Brothers (LEH, sec_id = SEC_LEH_001) delisted 2008-09-15.

**Canary 04 Validates:**
- 2008-08-01 observation: LEH is member = TRUE ✓
- 2008-09-15 observation: LEH is member = TRUE ✓ (still a member on delisting date)
- 2009-01-01 observation: LEH is member = FALSE ✓ (no longer a member after delisting)

---

## Canary 05: Filing Lag Window

**Defect Class:** Temporal Boundary Confusion

### The Problem

The fiscal period-end date is NOT the date the data became known. 10-Q/10-K filings are published weeks or months later. Naive systems join on fiscal period-end, allowing observations before publication to see the future.

```
Timeline: Filing Lag Window

Q1 2023 Period-End: 2023-03-31
                       │
                       ├─→ 40-day lag (regulatory minimum)
                       │
                       ▼
         Q1 2023 10-Q Filed: 2023-05-10 16:30 UTC
                       │
                       ▼ Naive: Join on 2023-03-31 (WRONG)
                         Q1 data visible from day 1 of Q1
                         
                       ▼ PIT Correct: Join on 2023-05-10 16:30 UTC
                         Q1 data visible only after filing published
```

### Test Assertion

**Scenario:** Q1 10-Q period-end 2023-03-31, filed 2023-05-10 16:30 UTC. Q4 filed 2023-01-27.

**Canary 05 Validates:**
- 2023-04-01 observation: Q4 visible, Q1 not yet visible ✓
- 2023-05-10 15:00 UTC observation: Q4 visible, Q1 not yet visible ✓
- 2023-05-10 17:00 UTC observation: Q4 still visible, Q1 now visible ✓
- Q1 remains visible until next quarter's Q2 filing

---

## Canary 06: Ticker Relabeling

**Defect Class:** Entity Identity Fragmentation

### The Problem

Companies change tickers (rebranding, delistings). Naive systems treat the ticker as the permanent identifier, fragmenting entity history across multiple identifiers.

```
Timeline: Ticker Relabeling

2022-06-09: Facebook → Meta Rebrand
       │
       ▼
       
Naive: Treat "FB" and "META" as separate entities
       → Meta's history starts at 2022-06-09
       → Facebook's history ends at 2022-06-09
       → No continuous feature engineering across rebrand
       
PIT: Use permanent sec_id (e.g., "SEC_META_001") throughout
     Both "FB" (until 2022-06-09) and "META" (from 2022-06-09)
     point to the same sec_id
```

### Test Assertion

**Scenario:** Facebook (FB) rebrands to Meta (META) on 2022-06-09. Both resolve to `SEC_META_001`.

**Canary 06 Validates:**
- 2022-06-08 observation: resolve("FB") → SEC_META_001 ✓
- 2022-06-09 observation: resolve("META") → SEC_META_001 ✓
- Features (momentum, RSI, etc.) are computed across entire sec_id history without fragmentation ✓

---

## Running the Canary Suite

```bash
# Run all 10 canaries (including 3 harness self-tests)
ledger canaries

# Run a specific canary
pytest tests/canaries/test_canary_01_restatements.py -v

# Run with verbose output and timing
pytest tests/canaries/ -v --durations=10
```

**Expected output:**
```
tests/canaries/test_canary_01_restatements.py::test_restatement_isolated_until_known_from PASSED
tests/canaries/test_canary_02_retroactive_splits.py::test_retroactive_split_adjustment_respects_observation_time PASSED
tests/canaries/test_canary_03_after_hours_session.py::test_after_hours_filing_shifts_to_next_open PASSED
tests/canaries/test_canary_04_survivorship_universe.py::test_survivorship_excludes_delisted_before_membership PASSED
tests/canaries/test_canary_05_filing_lag_window.py::test_filing_lag_prevents_prior_quarter_leakage PASSED
tests/canaries/test_canary_06_ticker_relabeling.py::test_ticker_rebrand_continuous_sec_id PASSED
tests/canaries/test_harness_self_test.py::test_builders_produce_expected_schemas PASSED
tests/canaries/test_harness_self_test.py::test_leaky_pipeline_diverges_from_truth PASSED
tests/canaries/test_harness_self_test.py::test_assertion_helpers_detect_divergence PASSED
tests/canaries/test_harness_self_test.py::test_leaky_same_day_filing_diverges PASSED

===================== 10 passed in 1.97s ======================
```

---

## Integration with Bitemporal Architecture

The canary suite validates Ledger's three core design decisions:

1. **Bitemporal Storage** (Canaries 01, 04, 05, 06)
   - `known_from` and `valid_to` enforce temporal correctness
   - Append-only raw storage prevents accidental updates

2. **Dynamic Feature Joining** (Canaries 02, 03, 05)
   - `join_features_as_of()` respects observation time boundaries
   - Corporate action adjustments (CAF) are computed at query time

3. **Entity Resolution** (Canary 06)
   - `sec_id` is permanent; ticker is an alias
   - Feature streams are continuous across rebranding

---

## References

- Implementation: [tests/canaries/](../tests/canaries/)
- Bitemporal model: [ADR-001: Append-Only Derived Bounds](adr/001-append-only-derived-bounds.md)
- ASOF engine: [ledger/features/engine.py](../ledger/features/engine.py)
- Entity resolution: [ledger/core/entity.py](../ledger/core/entity.py)

---

**Status:** Week 5 Day 3. All 10 canaries pass in deterministic ~2s runtime. Production-ready validation of point-in-time correctness.
