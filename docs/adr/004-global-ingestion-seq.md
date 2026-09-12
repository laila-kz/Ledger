# ADR-004: Monotonic Global Ingestion Sequence for Deterministic Window Ordering

* **Status:** Accepted
* **Date:** 2026-09-12
* **Authors:** Ledger Quantitative Data Engineering Team
* **Deciders:** Core Architecture Team
* **Technical Domain:** Storage Layer & Bitemporal Ordering

---

## 1. Context & Problem Statement

In an append-only bitemporal system, upper bounds (`known_to`, `valid_to`) are derived at query time using window functions:
```sql
LEAD(known_from) OVER (
    PARTITION BY sec_id, metric_name 
    ORDER BY known_from, ingestion_seq
) AS known_to
```

However, ordering strictly by timestamp (`ORDER BY known_from`) introduces fatal edge cases:
1. **Sub-Millisecond Collisions & Batch Overlaps:** Multiple corrections or batch loads processed during backfills or automated pipelines can share identical timestamps.
2. **Clock Skew & Timestamp Drift:** Physical machine clocks are non-monotonic and prone to micro-adjustments (NTP sync).
3. **Non-Deterministic Window Results:** If two rows have identical `known_from` timestamps, SQL engines (DuckDB, Spark, Polars) produce non-deterministic `LEAD()` ordering across runs, resulting in flaky backtest results and irreproducible Sharpe ratios.

We require a deterministic, monotonic tiebreaker embedded in every written row and filename.

---

## 2. Decision

> **We enforce a system-wide monotonic integer counter `ingestion_seq` allocated per batch and persisted in `metadata/ingestion_log.parquet`. Every raw row stores `ingestion_seq`, and all bitemporal window functions mandate `ORDER BY timestamp, ingestion_seq`.**

### Key Architectural Rules

1. **Batch Allocation & Monotonic Increment:**
   * The `IngestionLogManager` increments `ingestion_seq` ($1, 2, 3, \dots$) atomically for each committed batch.
   * Every batch generates partition files named:
     `batch_{ingestion_seq:06d}_{uuid}.parquet`
2. **Immutable Append Invariant:**
   * Re-running or backfilling an ingestion batch never mutates existing files; it creates a new batch with a higher `ingestion_seq`.
   * The latest batch naturally takes precedence in window queries because its higher sequence number sorts after earlier runs.
3. **Audit Trail & Lineage Coupling:**
   * Every batch logged in `ingestion_log.parquet` records the timestamp, source, total row count, file count, and cryptographic SHA-256 digests for all generated Parquet partition files.
   * This audit log serves as the foundation for Week 4 content-addressable run manifests (`manifest.json`).

---

## 3. Consequences

### Positive (Gains & Guarantees)
* **100% Deterministic Window Ordering:** Window functions `LEAD()` and `LAG()` produce mathematically identical output across different OS platforms, hardware architectures, and engine versions.
* **Complete Auditability:** Every row in storage can be traced back to the exact batch, timestamp, source, and commit SHA that produced it.
* **Safe Re-ingestion / Backfills:** Buggy historical data can be superseded cleanly by appending a corrected batch with a higher sequence number.

### Negative & Trade-offs (Liabilities & Mitigations)
* **Central Sequence Coordination:** Sequence allocation requires reading the current max sequence from the ingestion log.
  * *Mitigation:* Ingestion batches are discrete analytical jobs (EOD daily or hourly), not high-frequency microsecond transactions. The atomic Parquet log writer handles sequential increments with sub-millisecond overhead.

---

## 4. Alternatives Considered & Rejected

### Option A: Timestamp-Only Ordering (`ORDER BY known_from`)
* **Description:** Rely solely on event/knowledge timestamps.
* **Why Rejected:** Non-deterministic when timestamps collide during batch backfills, leading to random windowing order and unreproducible backtest results.

### Option B: UUID / Hash Sorting
* **Description:** Sort by row hash or UUID string.
* **Why Rejected:** UUIDs do not preserve causality or chronological batch submission order.

### Option C: Per-Table or Per-Ticker Local Counters
* **Description:** Maintain independent sequence counters per partition directory or ticker.
* **Why Rejected:** Prevents global cross-table join synchronization and significantly complicates multi-asset data lineage manifests.

---

## 5. References & Prior Art
* Leslie Lamport, *Time, Clocks, and the Ordering of Events in a Distributed System* (CACM, 1978).
* Martin Fowler, *Event Sourcing and Monotonic Sequences* (2005).
* ADR-001: Append-Only Immutable Storage with Derived Upper Bounds.
