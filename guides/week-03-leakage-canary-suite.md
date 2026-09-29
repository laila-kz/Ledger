# Week 3 Guide: The Leakage Canary Suite (Core Deliverable)

**Primary Objective:** Build and verify the Leakage Canary Suite — the core technical differentiator of this portfolio project. Implement synthetic bitemporal test fixtures, write programmatic assertions for all 6 look-ahead defect scenarios, and establish automated CI validation.

---

## 1. Definition of Done (DoD) for Week 3
- [ ] Synthetic test fixtures and mock scenario generator built in `tests/canaries/conftest.py`.
- [ ] Tier 1 Canaries implemented and verified passing:
  - [ ] Canary 01: Restated Fundamentals (`test_canary_01_restatements.py`).
  - [ ] Canary 02: Retroactive Split Adjustment (`test_canary_02_retroactive_splits.py`).
  - [ ] Canary 03: After-Hours & Exchange Sessions (`test_canary_03_after_hours_session.py`).
  - [ ] Canary 05: Statutory Filing Lag Window (`test_canary_05_filing_lag_window.py`).
- [ ] Tier 2 Canaries implemented (or queued as explicit fallback):
  - [ ] Canary 04: Survivorship Universe Exclusion (`test_canary_04_survivorship_universe.py`).
  - [ ] Canary 06: Ticker Re-identification Alias Drift (`test_canary_06_ticker_relabeling.py`).
- [ ] `pytest tests/canaries/` executes deterministically and outputs a clean test report.

---

## 2. Day-by-Day Implementation Plan

### Day 1: Synthetic Canary Harness (`conftest.py`)
1. **Build Mock Scenario Fixtures:**
   - Create deterministic test data generators for:
     - Synthetic raw OHLCV price series.
     - Synthetic split events (e.g. 4:1 split on 2020-08-31 with announcement on 2020-08-11).
     - Synthetic fundamental filings with original vs. amended restatements.
     - Synthetic entity maps with ticker renames.
2. **Implement Canary Assertion Utilities:**
   - Helper `assert_no_lookahead(pipeline_result, reference_truth)` comparing leaky vs. PIT output series.

### Day 2: Tier 1 Canaries (Canary 01 & Canary 02)
1. **Canary 01: Restated Fundamentals (`test_canary_01_restatements.py`)**
   - **Scenario:** Q2 EPS originally filed on 2023-08-01 as \$1.00 (`known_from = 2023-08-01 17:30`). Restated on 2023-11-15 to \$0.70 (`known_from = 2023-11-15 17:30`).
   - **Assertion:** 
     - Querying as of 2023-09-01 returns strictly \$1.00 (`known_to` is 2023-11-15).
     - Querying as of 2023-12-01 returns \$0.70.
     - Assert leaky pipeline that queries without `known_to` fails with assertion error.
2. **Canary 02: Retroactive Split Adjustment (`test_canary_02_retroactive_splits.py`)**
   - **Scenario:** Stock trades at \$400 in July 2020. 4:1 split takes effect 2020-08-31.
   - **Assertion:**
     - Querying as of 2020-07-15 returns raw price \$400 and $\text{CAF} = 1.0$.
     - Querying as of 2020-09-01 returns pre-split price \$100 and $\text{CAF} = 4.0$.
     - Assert that naive pre-adjusted price is caught and rejected prior to 2020-08-31.

### Day 3: Tier 1 Canaries (Canary 03 & Canary 05)
1. **Canary 03: After-Hours / Exchange Session (`test_canary_03_after_hours_session.py`)**
   - **Scenario:** Company releases material earnings on Friday 2023-04-14 at 17:00 ET (post-market close).
   - **Assertion:**
     - A rebalance observation at Friday 15:59:00 ET cannot see this filing.
     - Feature engine shifts actionable knowledge timestamp to Monday 2023-04-17 at 09:30:00 ET open.
     - Assert look-ahead detection if filing is accessed during Friday's trading session.
2. **Canary 05: Filing Lag Window (`test_canary_05_filing_lag_window.py`)**
   - **Scenario:** Fiscal quarter ends 2023-03-31. 10-Q filing accepted on SEC EDGAR on 2023-05-10.
   - **Assertion:**
     - Querying as of 2023-04-15 returns `NULL` or previous quarter (Q4) data, not Q1 data.
     - Assert that naive $T+0$ fiscal period joins are caught and flagged as leakage.

### Day 4: Tier 2 Canaries (Canary 04 & Canary 06)
1. **Canary 04: Survivorship Universe Exclusion (`test_canary_04_survivorship_universe.py`)**
   - **Scenario:** Bankrupt financial entity (e.g. `LEHMQ`) delisted in September 2008.
   - **Assertion:**
     - Point-in-time universe query as of 2008-08-01 includes `LEHMQ`.
     - Point-in-time universe query as of 2009-01-01 excludes `LEHMQ`.
     - Assert that static modern S&P500 constituent lists fail the test.
2. **Canary 06: Ticker Re-identification Alias Drift (`test_canary_06_ticker_relabeling.py`)**
   - **Scenario:** Entity `SEC_0001326801` traded as `FB` until 2022-06-09, when it rebranded to `META`.
   - **Assertion:**
     - Querying as of 2018 resolves `sec_id` and maps historical symbol `FB`.
     - Joining features across the rebrand boundary preserves continuity without data loss or duplicate keys.

### Day 5: Test Suite Automation & Documentation
1. **Implement `docs/canary_catalog.md`:**
   - Document each of the 6 canaries: Defect mechanism, why naive pipelines fail, and the exact assertion preventing it.
2. **Run Full Test Suite:**
   - Execute `pytest -v tests/canaries/`.
   - Verify all 6 tests pass deterministically in $< 5$ seconds.

---

## 3. Key Architectural Traps to Avoid in Week 3
- **Trap 1:** Writing tests that only test happy paths. A canary test must verify that a deliberately introduced bug **actually fails** when run through the leaky pipeline, and **passes** through Ledger.
- **Trap 2:** Mocking away the core logic. Fixtures should generate realistic Arrow/Parquet tables that pass through the actual `join_features_as_of` engine.
- **Trap 3:** Non-deterministic timestamps. Always use timezone-aware UTC timestamps (`datetime.now(timezone.utc)`) or explicit ISO-8601 strings with timezone offsets.
