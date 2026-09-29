# ADR-001: Append-Only Immutable Storage with Derived Upper Bounds

* **Status:** Accepted
* **Date:** 2026-09-12
* **Authors:** Ledger Quantitative Data Engineering Team
* **Deciders:** Core Architecture Team
* **Technical Domain:** Storage Layer & Bitemporal Interval Engine

---

## 1. Context & Problem Statement

In quantitative trading feature stores, financial data contains two orthogonal temporal dimensions:
1. **Valid Time (Market Reality):** When an economic fact occurred in the real world (e.g. fiscal quarter ended June 30).
2. **Transaction Time (Knowledge Time):** When that fact was recorded and became actionable to the trading system (e.g. 10-Q filed and accepted on SEC EDGAR on August 8 at 17:30 ET).

When restatements, retroactive amendments, or ticker renames occur, standard data warehousing approaches mutate existing records (e.g. updating a row's `is_current = FALSE` and writing a new row, or updating a physical `known_to` timestamp column). 

However, in an analytical feature store built on columnar immutable formats (Apache Parquet) and queried through analytical engines (DuckDB / Polars):
- **Parquet files are inherently immutable.** In-place mutation requires rewriting entire partition files or maintaining heavy delta/ACID transactional overlays.
- **Physical closed bounds (`known_to`, `valid_to`) create write amplification.** When a new restatement arrives at $T_2$ for a record known since $T_1$, writing $T_2$ into the previous record's `known_to` column mutates historical Parquet blocks.
- **Race conditions and historical corruption.** Any mutation risks corrupting historical reproducibility and breaks SHA-256 content-addressable lineage.

We need a design where all raw ingestion files remain strictly append-only and immutable forever, while guaranteeing mathematically exact bitemporal point-in-time point queries and vectorized ASOF joins.

---

## 2. Decision

> **We will enforce strictly append-only raw Parquet storage storing only lower bounds (`known_from` and `valid_from`) alongside a monotonic `ingestion_seq` tiebreaker. Upper bounds (`known_to` and `valid_to`) will be derived dynamically at query time using vectorized window functions (`LEAD()`).**

### Key Architectural Invariants
1. **Zero Row Mutations:** No raw Parquet file or row is ever modified, overwritten, or deleted. Ingesting a restatement or symbol change simply appends a new row with its own `known_from` / `valid_from` and an incremented `ingestion_seq`.
2. **Deterministic Sequence Ordering:** Ingestion assigns a monotonic integer `ingestion_seq` per batch to deterministically resolve any simultaneous or same-millisecond transactions.
3. **Derived Knowledge Interval Formulation:**
   ```sql
   CREATE VIEW v_bitemporal_fundamentals AS
   SELECT 
       sec_id,
       filing_type,
       fiscal_period_end,
       metric_name,
       metric_value,
       known_from,
       LEAD(known_from) OVER (
           PARTITION BY sec_id, metric_name, fiscal_period_end 
           ORDER BY known_from, ingestion_seq
       ) AS known_to
   FROM fact_fundamentals_raw;
   ```
4. **Point-in-Time Point-Query Invariant:**
   A fact is visible to observation timestamp $T_{obs}$ if and only if:
   $$\text{known\_from} \le T_{obs} < \text{COALESCE}(\text{known\_to}, +\infty)$$
5. **Selective Versioning Scope:**
   Only entities subject to statutory revisions (fundamentals, SEC filings, entity ticker mappings) require windowed interval derivation. Price bars (`fact_market_ohlcv_raw`) and corporate actions are immutable per `(sec_id, trade_date)` and do not require `LEAD()` overhead.

---

## 3. Consequences

### Positive (Gains & Guarantees)
* **True Immutability & Replayability:** Historical datasets are byte-stable. Parquet partition files can be cached, hashed (SHA-256), and replayed with 100% mathematical determinism.
* **Zero Write Amplification:** Appending a restatement is an $O(1)$ append of new records into the latest partition; no historical files are scanned or rewritten.
* **No Database Locking / Distributed Locks:** Ingestion writers do not need distributed locks to update prior state.
* **Vectorized Execution:** DuckDB and Polars compute windowed `LEAD()` across millions of rows in milliseconds using vectorized SIMD execution.

### Negative & Trade-offs (Liabilities & Mitigations)
* **Query-Time Window Overhead:** Evaluating `LEAD()` at query time incurs a sorting/windowing step.
  * *Mitigation:* Partitions are partitioned by `/year=YYYY/month=MM/` or filtered by `sec_id`, keeping partition cardinality small. DuckDB push-down predicate evaluation filters partitions prior to window execution.
* **View Abstraction Discipline:** Strategy developers must query through canonical bitemporal views or `join_features_as_of()` rather than raw unwindowed tables directly.

---

## 4. Alternatives Considered & Rejected

### Option A: In-Place Row Mutation (Update existing `known_to` column)
* **Description:** When restatement $R_2$ arrives, execute `UPDATE table SET known_to = R2.known_from WHERE ...` on the prior record.
* **Why Rejected:** Parquet does not support in-place row updates. Rewriting historical Parquet files breaks cryptographic SHA-256 data lineage manifests, introduces race conditions during concurrent backtests, and risks silent data corruption.

### Option B: Traditional SCD Type 2 with `is_current` Flag
* **Description:** Store `is_current = TRUE/FALSE` and mutate the flag upon revision.
* **Why Rejected:** `is_current` only answers "what is true today?", which is the exact anti-pattern causing look-ahead bias in quant backtests. It cannot answer "what was believed on October 14, 2021?".

### Option C: Delta Lake / Apache Iceberg ACID Table Formats
* **Description:** Use ACID lakehouse transaction logs with merge-on-read or copy-on-write tables.
* **Why Rejected:** Introduces heavy JVM/Spark/Java dependency bloat and operational infrastructure complexity. Ledger is designed to be lean, local-first, zero-overhead, and directly runnable in pure Python/DuckDB/Parquet environments.

---

## 5. References & Prior Art
* Richard T. Snodgrass, *Developing Time-Oriented Database Applications in SQL* (Morgan Kaufmann, 1999).
* Martin Fowler, *Bitemporal History* (2005).
* Ledger Architectural Blueprint: `guides/ledger-blueprint.md`.
