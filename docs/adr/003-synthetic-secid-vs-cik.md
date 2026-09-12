# ADR-003: Synthetic Permanent SecID vs. SEC CIK or Ticker

* **Status:** Accepted
* **Date:** 2026-09-12
* **Authors:** Ledger Quantitative Data Engineering Team
* **Deciders:** Core Architecture Team
* **Technical Domain:** Core Entity Resolution & Data Modeling

---

## 1. Context & Problem Statement

Financial data pipelines require an immutable primary key to join price time-series, corporate actions, and fundamental filings without corrupting historical lineage. However, identifying securities across decades presents several fundamental traps:

1. **The Ticker Myth:** Ticker symbols are mutable display labels, not identities. When Facebook rebranded `FB` $\to$ `META` on 2022-06-09, naive ticker-keyed pipelines fragmented historical prices into two disjoint assets. Furthermore, exchanges regularly reassign delisted tickers to unrelated companies years later (ticker reuse).
2. **The CIK Multi-Class Trap:** SEC CIK (Central Index Key) identifies corporate legal entities (issuers), not individual tradable securities. A single corporate issuer often has multiple distinct tradable share classes with separate pricing, voting rights, and dividends:
   * Alphabet Inc. (CIK `0001652044`): `GOOGL` (Class A voting) vs. `GOOG` (Class C non-voting).
   * Berkshire Hathaway Inc. (CIK `0001067983`): `BRK.A` vs. `BRK.B`.
3. **Proprietary Identifier Licensing:** Identifiers like CUSIP and SEDOL carry restrictive legal licensing fees, redistribute restrictions, and auditing penalties (e.g., S&P / FactSet licensing).

We must establish the canonical security identifier architecture for Ledger.

---

## 2. Decision

> **We will assign a permanent, immutable synthetic `sec_id` to each unique tradable security upon ingestion. Tickers, CIKs, and FIGIs are modeled as time-varying attributes linked through the bitemporal entity mapping layer (`dim_security_ticker_raw`).**

### Key Architectural Rules

1. **Immutability of `sec_id`:**
   * A `sec_id` is assigned once and never changes throughout the life of the instrument.
   * Internal storage tables (`fact_market_ohlcv_raw`, `fact_corporate_actions`, `fact_fundamentals_raw`) are strictly partitioned and keyed by `sec_id`, never by raw ticker.

2. **Decoupled Bitemporal Mapping (`dim_security_ticker_raw`):**
   * Symbol renames (e.g. `FB` $\to$ `META`) are recorded as append-only rows in `dim_security_ticker_raw` with a `valid_from` timestamp.
   * The active `valid_to` bound is derived dynamically via `LEAD(valid_from) OVER (PARTITION BY sec_id ORDER BY valid_from, ingestion_seq)`.

3. **Deterministic Query Contract:**
   * Callers query the bitemporal view `v_bitemporal_ticker_map` via `resolve_sec_id(ticker, as_of_date)`:
     ```sql
     SELECT sec_id FROM v_bitemporal_ticker_map
     WHERE UPPER(ticker) = ? 
       AND valid_from <= ? 
       AND (valid_to IS NULL OR valid_to > ?)
     ```
   * Querying `('FB', '2020-01-01')` and `('META', '2023-01-01')` deterministically return the same `SEC_META` identifier.

---

## 3. Consequences

### Positive (Gains & Guarantees)
* **Zero Fragmentation Across Renames:** Full historical price series remain contiguous and intact before and after corporate rebranding.
* **Support for Multi-Class Share Structures:** `GOOGL` (`SEC_GOOGL_A`) and `GOOG` (`SEC_GOOGL_C`) share the same issuer CIK while maintaining independent price and dividend time series.
* **Ticker Reuse Isolation:** If ticker `XYZ` was used by Old Corp in 2012 and New Corp in 2022, point-in-time queries resolve to `SEC_OLD_CORP` and `SEC_NEW_CORP` respectively without cross-contamination.
* **Open Source & License-Free:** Synthetic IDs do not require CUSIP / SEDOL licensing.

### Negative & Trade-offs (Liabilities & Mitigations)
* **Translation Hop at Ingestion & Ingress:** Users submitting backtests with human-readable tickers must resolve them to `sec_id` before querying feature views.
  * *Mitigation:* The `resolve_sec_id()` helper and vectorized ingestion mapper resolve ticker batches in milliseconds.

---

## 4. Alternatives Considered & Rejected

### Option A: Raw Ticker Symbol as Storage Key
* **Description:** Key all database tables and Parquet partitions by ticker (e.g. `/ticker=AAPL/`).
* **Why Rejected:** Causes fatal look-ahead bias and breaks historical continuity upon symbol renames, mergers, or ticker reassignments.

### Option B: SEC CIK as Primary Storage Key
* **Description:** Key tables by 10-digit CIK string.
* **Why Rejected:** Collapses multiple share classes (`GOOG` vs `GOOGL`, `BRK.A` vs `BRK.B`) into a single key, creating price and volume collisions. Fails for non-SEC assets (ETFs, crypto, international equities).

### Option C: Commercial Identifiers (CUSIP / SEDOL)
* **Description:** Key tables by 9-character CUSIP.
* **Why Rejected:** Prohibitive licensing restrictions, commercial vendor lock-in, and legal risk for open-source reproducible research.

---

## 5. References & Prior Art
* Center for Research in Security Prices (CRSP) `PERMNO` / `PERMCO` architecture.
* OpenFIGI Standard (Bloomberg).
* Ledger Leakage Canary Suite: `tests/canaries/test_canary_06_ticker_relabeling.py`.
