# ADR-0001: Bitemporal Interval Model with Append-Only Derived Bounds

* **Status:** Accepted
* **Date:** 2026-09-14
* **Authors:** Ledger Quantitative Data Engineering Team
* **Deciders:** Core Architecture Team
* **Technical Domain:** Storage Layer & Bitemporal Interval Engine

---

## 1. Context & Problem Statement

Quantitative strategy research requires modeling financial reality across two independent time axes:
1. **Valid Time (Effective Market Reality):** When an economic or corporate event took place in the real world (e.g., fiscal quarter ended June 30, dividend ex-date September 1).
2. **Transaction Time (System Knowledge Time):** When that data became available and actionable to market participants (e.g., 10-Q filing accepted on SEC EDGAR on August 8 at 17:30 UTC).

Traditional data warehouse patterns for bitemporality mutate historical records in place (e.g., setting `is_current = FALSE` and closing the open interval by writing a physical `known_to` timestamp into old rows). In an immutable analytical data lake built on Apache Parquet and queried via DuckDB/Polars:
- **Parquet files are physically immutable.** Mutating an existing record requires rewriting whole parquet files or maintaining complex ACID delta log transaction layers.
- **Physical closed bounds (`known_to`) cause write amplification.** When a statutory restatement arrives at $T_2$ for a historical filing published at $T_1$, writing $T_2$ into the previous record's `known_to` column mutates historical partitions, breaking cryptographic file hashes.
- **Race conditions and historical corruption.** Any in-place mutation risks lookahead corruption and invalidates content-addressable backtest lineage.

We require a storage and interval model that is strictly append-only, has zero physical file mutation, and deterministically reconstructs exact point-in-time point queries and interval joins.

---

## 2. Decision

> **We enforce strictly append-only raw Parquet storage containing only lower bounds (`known_from` and `valid_from`) alongside a monotonic global sequence number (`ingestion_seq`). Upper bounds (`known_to` and `valid_to`) are derived dynamically at query time using vectorized window functions (`LEAD()`).**

### Core Architectural Invariants

1. **Zero Row Mutations:** Raw Parquet partitions are write-once, append-only. When a corporate restatement or revision arrives, a new record is appended with its own `known_from` and an incremented `ingestion_seq`.
2. **Deterministic Sequence Tiebreaking:** Every batch ingestion generates a strictly increasing `ingestion_seq`. If two records share identical timestamps, `ingestion_seq` deterministically dictates revision order.
3. **Dynamic Interval Derivation via SQL Window Functions:**
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
           ORDER BY known_from ASC, ingestion_seq ASC
       ) AS known_to
   FROM fact_fundamentals_raw;
   ```
4. **Point-in-Time Point-Query Invariant:**
   A fact is visible to observation timestamp $T_{\text{obs}}$ if and only if:
   $$\text{known\_from} \le T_{\text{obs}} < \text{COALESCE}(\text{known\_to}, +\infty)$$

5. **Asymmetric Interval Boundaries:**
   - Left-closed, right-open: $[\text{known\_from}, \text{known\_to})$.
   - If a restatement occurs at exactly $16:00:00.000$, queries at $15:59:59.999$ observe the original value, while queries at $16:00:00.000$ observe the revised value.

---

## 3. Consequences

### Positive (Gains & Guarantees)
- **Mathematical Immutability:** Historical Parquet files remain bit-for-bit identical forever, enabling deterministic SHA-256 hashing in audit manifests.
- **Zero Write Amplification:** Appending a restatement requires writing only the new row batch, not scanning and updating legacy files.
- **Flawless Point-in-Time Point Queries:** Zero risk of retroactive revision leakage.

### Negative & Trade-offs (Liabilities & Mitigations)
- **Query Overhead for Window Functions:** Calculating `LEAD()` requires partition scanning.
  - *Mitigation:* Partition data by `sec_id` and fiscal year in Parquet; DuckDB pushes projection and filter predicates down to parquet metadata, computing `LEAD()` over micro-partitions in $< 5\text{ms}$.

---

## 4. Alternatives Considered & Rejected

### Option A: Stored Physical Closed Bounds (`known_from`, `known_to`)
- **Why Rejected:** Ingesting a restatement requires reading the previous record, updating its `known_to` timestamp, and re-writing the Parquet file. This breaks cryptographic hash verification and creates massive I/O amplification.

### Option B: Delta Lake / Apache Iceberg ACID Overlays
- **Why Rejected:** Introduces heavy JVM/Rust metadata catalog dependencies, commit conflicts, and vacuuming complexities unnecessary for local or S3-backed quant feature stores.

---

## 5. References & Prior Art
- Snodgrass, R. T. (1999). *Developing Time-Oriented Database Applications in SQL*. Morgan Kaufmann.
- Ledger ADR-001: Append-Only Immutable Storage with Derived Upper Bounds.
