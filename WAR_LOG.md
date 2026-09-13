# WAR LOG: Quantitative Data Engineering Journal

> **Purpose:** Running engineering journal recording non-trivial technical decisions, debugging breakthroughs, edge cases, surprises, and reversals throughout the build of Ledger.

---

## [2026-09-12] Day 1: Project Scaffolding, Strict Quality Gates & Append-Only Storage Core

### 1. The Architectural Thesis: Why Ledger Exists
* **Context:** Most quant backtesting frameworks succumb to look-ahead bias through static survivor-biased universes, retroactive split adjustments, and unversioned fundamental restatements.
* **The Core Invariant:** Raw storage is strictly append-only. Only lower bounds (`known_from`, `valid_from`) and a monotonic `ingestion_seq` are stored. Upper bounds (`known_to`, `valid_to`) are derived dynamically at query time using `LEAD()` window functions over `(valid_from, ingestion_seq)`.
* **ADR Recorded:** Established [ADR-001: Append-Only Immutable Storage with Derived Upper Bounds](file:///docs/adr/001-append-only-derived-bounds.md).

### 2. Environment & Tooling Setup
* **Decision:** Python 3.10+ support with `pydantic>=2.0`, `duckdb`, `polars`, `pyarrow`, `exchange_calendars`, `yfinance`, `pytest`, `ruff`, and `mypy` in strict mode.
* **Quality Gates Configured:**
  * `tool.ruff`: Configured with rules `E`, `W`, `F`, `I` (isort), `N` (naming), `UP` (pyupgrade), `B` (bugbear), `A` (builtins), `C4` (comprehensions), `SIM` (simplify). Line length capped at 100.
  * `tool.mypy`: Configured with `strict = true`, `disallow_untyped_defs = true`, `disallow_incomplete_defs = true`, `plugins = ["pydantic.mypy"]`.
  * `pytest`: Configured with `pythonpath = ["."]`, `testpaths = ["tests"]`.

### 3. Obstacles & Surprises Encountered
* **Pip Download Temp Lock on Windows:** When pip encountered network retries while downloading larger wheels (e.g. `duckdb`), Windows file handles on temporary unpack folders threw `[WinError 32] PermissionError` during pip's internal resume attempts.
  * *Resolution:* Applied `--timeout 180 --retries 10` flags and installed clean distributions into virtual environment without relying on broken resume locks.
* **Ruff Import Sorting Enforcement:** Ruff flagged unsorted imports across initial test stubs. Fixed and automated via `ruff check --fix` and unified `Makefile`.

### 4. Git & Branching Strategy
* **Branch:** Feature development isolated on `Work-in-progress`.
* **CI Strategy:** GitHub Actions workflow (`.github/workflows/ci.yml`) triggers on every push to validate Ruff linting, MyPy strict type checks, and Pytest test suite execution.

---

## [2026-09-12] Day 2: Bitemporal Intervals, Window Derivations & NYSE Session Calibrations

### 1. Bitemporal Interval Architecture (`ledger/core/bitemporal.py`)
* **Mathematical Invariant:** `ValidInterval` $[valid\_from, valid\_to)$ and `TransactionInterval` $[known\_from, known\_to)$ enforce half-open interval semantics ($t \in [start, end) \iff start \le t < end$).
* **Window Derivation Implementation:**
  * Created `derive_known_to_polars` and `derive_valid_to_polars` using `pl.col(...).shift(-1).over(partition_cols)`.
  * Generated dynamic DuckDB SQL derivation helper `build_bitemporal_view_sql()` injecting `LEAD(known_from) OVER (PARTITION BY ... ORDER BY known_from, ingestion_seq) AS known_to`.
  * Implemented `filter_point_in_time_polars` strictly enforcing:
    $$\text{known\_from} \le T_{obs} \land (\text{known\_to IS NULL} \lor T_{obs} < \text{known\_to})$$

### 2. Exchange Calendar & Actionable Time Resolver (`ledger/core/calendars.py`)
* **NYSE Calendar (`XNYS`):** Encapsulated exchange hours, early session closes (e.g. 13:00 on Black Friday), and holidays (e.g. Juneteenth).
* **Actionable Timestamps:**
  * **EOD Bars (`is_market_data=True`):** 15-minute post-market buffer (16:00 close $\to$ 16:15 EST actionable; 13:00 early close $\to$ 13:15 EST actionable).
  * **Filings/Reports (`is_market_data=False`):** In-session releases actionable immediately; after-hours (e.g. Friday 17:00 EST), pre-market, or weekend releases roll forward to next session market open (**09:30 EST**).

### 3. Unit Test Coverage
* Unit tests created in `tests/unit/test_bitemporal.py` and `tests/unit/test_calendars.py`.

---

## [2026-09-12] Day 3: Entity Resolution & Permanent Identifiers (SecID Architecture)

### 1. The Ticker Identity Problem
* **The Insight:** Tickers are ephemeral display labels, not stable entity identities. Renames (`FB` $\to$ `META` on 2022-06-09), multi-share class equity structures (`GOOG` non-voting vs `GOOGL` voting under Alphabet CIK `0001652044`), and ticker reassignments cause silent data loss or look-ahead bias if keyed by ticker.
* **The Solution:** Permanent synthetic `sec_id` as primary storage key, mapped to tickers via append-only `dim_security_ticker_raw` and derived `v_bitemporal_ticker_map`.
* **ADR Recorded:** Established [ADR-003: Synthetic Permanent SecID vs. SEC CIK or Ticker](file:///docs/adr/003-synthetic-secid-vs-cik.md).

### 2. Implementation Details (`ledger/core/entity.py`)
* `SecurityTickerMapping`: Pydantic model enforcing `(sec_id, ticker, valid_from, ingestion_seq)`.
* `resolve_sec_id(ticker, as_of, conn/df)`: Bitemporal resolution matching $valid\_from \le as\_of < valid\_to$.
* `resolve_ticker(sec_id, as_of, conn/df)`: Inverse resolution finding active ticker label for a `sec_id` on a given date.
* `register_ticker_map_view(conn)`: DuckDB SQL view creation using `LEAD(valid_from) OVER (PARTITION BY sec_id ORDER BY valid_from, ingestion_seq) AS valid_to`.

### 3. Unit Tests (`tests/unit/test_entity.py`)
* Verified FB $\to$ META rename scenario (`resolve_sec_id('FB', '2020-01-01') == resolve_sec_id('META', '2023-01-01') == 'SEC_META'`).
* Verified exact boundary transition on 2022-06-09.
* Verified ticker reuse scenario between disjoint historical entities.

---

## [2026-09-12] Day 4: Storage Layer, Hive Parquet Partitioning & Ingestion Logging

### 1. The Storage Problem & Physical Invariants
* **Layout Design:**
  - `data/raw/market_ohlcv/year=YYYY/month=MM/batch_{ingestion_seq}_{uuid}.parquet` (monthly due to high daily density).
  - `data/raw/corporate_actions/year=YYYY/batch_{ingestion_seq}_{uuid}.parquet` (yearly due to sparse events).
  - `data/raw/entity_map/batch_{ingestion_seq}_{uuid}.parquet` (flat layout).
* **Atomic File Writes:** Parquet files are written to `.tmp_{uuid}_{name}` before atomic `os.replace()` to prevent corrupted partial reads.
* **Monotonic Global Sequence Counter:** Every batch is assigned a unique `ingestion_seq` tracked in `metadata/ingestion_log.parquet`.
* **ADR Recorded:** Established [ADR-004: Monotonic Global Ingestion Sequence for Deterministic Window Ordering](file:///docs/adr/004-global-ingestion-seq.md).

### 2. Implementation Modules
* `ledger/storage/partitions.py`: Partition layout helpers, atomic Parquet writer, SHA-256 hash calculator.
* `ledger/storage/ingestion_log.py`: Monotonic sequence generator and batch audit log manager (`IngestionLogManager`).
* `ledger/storage/catalog.py`: `LedgerCatalog` managing DuckDB connection, registering Hive-partitioned views and derived bitemporal views with empty placeholder fallbacks, and zero-copy Polars query execution.

### 3. Unit Test Coverage (`tests/unit/test_storage.py`)
* Verified atomic writes, SHA-256 hash validation, monthly/yearly Hive directory partitioning.
* Verified monotonic sequence increments across multiple batches and audit log persistence.
* Verified DuckDB catalog view registration with both empty schemas and populated partition datasets.

---

## [2026-09-12] Day 5: Ingestion Pipelines & Week 1 Definition of Done (DoD) Verification

### 1. The Ingestion Trap & Unadjusted Price Invariant
* **The Silent Adjustment Trap:** Modern `yfinance` returns split/dividend pre-adjusted prices by default. If ingested blindly, historical pre-split prices are retroactively compressed (e.g. AAPL July 2020 prices shown as $125 instead of $500), causing look-ahead leakage.
* **The Solution:**
  - Mandatory `auto_adjust=False, actions=False` in `yf.download`.
  - Discard `Adj Close` column completely and store only raw `Open, High, Low, Close, Volume`.
  - Corporate actions (`fact_corporate_actions`) ingested separately from `.splits` and `.dividends` series.
  - `known_from` convention: `ex_date` session close + 15 min buffer (16:15 EST / EDT converted to UTC).
* **ADR Recorded:** Established [ADR-005: Corporate Action Timing and Known-From Resolution Convention](file:///docs/adr/005-corporate-action-known-from-convention.md).

### 2. Implementation Modules & CLI Tools
* `ledger/ingestion/market_data.py`: `ingest_ohlcv()` pipeline with `TickerRegistry` (mapping tickers to `SEC_<TICKER>_001`), atomic monthly Parquet writing, and CLI interface (`python -m ledger.ingestion.market_data`).
* `ledger/ingestion/corporate_actions.py`: `ingest_corporate_actions()` pipeline for splits and cash dividends with yearly Hive partitioning and CLI interface (`python -m ledger.ingestion.corporate_actions`).
* `scripts/seed_week1.py`: Automated seed and verification script asserting pre-split price invariants (AAPL > $400 on 2020-08-28; TSLA > $1,800 on 2020-08-28).

### 3. Week 1 Definition of Done (DoD) Check
- [x] Development environment configured with Python 3.10+, Ruff, and MyPy in strict mode.
- [x] Bitemporal interval math module implemented and tested (`ledger/core/bitemporal.py`).
- [x] Exchange calendar wrapper operational for NYSE market hours (`ledger/core/calendars.py`).
- [x] Permanent SecID $\leftrightarrow$ ticker bitemporal resolver implemented (`ledger/core/entity.py`).
- [x] Parquet partition write/read utilities and DuckDB catalog built (`ledger/storage/`).
- [x] Raw unadjusted OHLCV and corporate actions ingestion pipelines built (`ledger/ingestion/`).
- [x] All 45 unit tests passing with 100% type-check clean (`mypy --strict`).

---

## [2026-09-13] Week 2 Day 5: Verification & Benchmarking

### 1. Performance Benchmark — Vectorized ASOF Join SLA

Benchmark: `benchmarks/bench_asof_engine.py --sweep` (warm-path, 5 timed runs, 2 warm-up)

| Tickers | Obs/Ticker | Total Obs | Mean ms | P99 ms | SLA |
| ------: | ---------: | --------: | ------: | -----: | :-- |
|       5 |        200 |     1,000 |    2.77 |   3.47 | **PASS** |
|      10 |        200 |     2,000 |    2.93 |   3.06 | **PASS** |
|      20 |        100 |     2,000 |    3.17 |   3.27 | **PASS** |
|      50 |         50 |     2,500 |    4.26 |   4.74 | **PASS** |
|     100 |         30 |     3,000 |    5.33 |   6.10 | **PASS** |

**Headline**: 5–6ms for 3,000 observations across 100 tickers on warm path — **20× under the 100ms SLA budget**.

Extrapolated capacity: 500 rebalances × 500 tickers = 250,000 obs → ~1.4 seconds total. A full backtesting grid fits in 2 seconds.

### 2. Apache Arrow Zero-Copy Verification

Benchmark: `benchmarks/bench_arrow_zero_copy.py --rows 100000 --runs 3`

| Stage | Latency |
| :---- | ------: |
| Polars → Arrow (`to_arrow`) | 1.1 ms |
| DuckDB register (in-memory) | 227 ms (cold DuckDB init) |
| DuckDB query → Arrow result | 8.9 ms |
| Arrow → Polars (`from_arrow`) | 0.6 ms |
| **Throughput** | **2.7M rows/sec** |

**Zero-copy confirmed at buffer-address level:**
- `Polars → Arrow buf shared : YES (zero-copy)` — `df["price"].to_arrow().buffers()[1].address == arrow_table.column("price").buffers()[1].address`
- `Input buf intact post-DDB : YES` — DuckDB does not copy or mutate the registered Arrow buffer.

**Note on DuckDB register latency:** The 227ms first-call cost is the DuckDB engine spin-up (schema inference, JIT compilation for the registered view). This amortizes to zero across a session — subsequent queries against the same registered view drop to <10ms. This matches ADR-009 warm-path exclusion.

### 3. Test Suite Expansion

| Module | New Tests | Coverage Added |
| :----- | --------: | :------------- |
| `tests/unit/test_arrow_zero_copy.py` | 10 | Buffer identity on `to_arrow()`, `from_arrow()`, DuckDB no-disk guarantee, 100k-row E2E |

**Total unit tests: 130 / 130 passing.**

### 4. Architectural Decisions Recorded

- **Canonical Temporal Coordinate:** Standardized strictly on `observation_timestamp` across all modules (`engine.py`, `registry.py`, `definitions/technical.py`, benchmarks, tests). Zero occurrences of `observation_ts` in the codebase.
- **[ADR-007](docs/adr/007-ema-seeding-convention.md):** EMA seeding uses Polars `adjust=False` convention (P₀-recursive), not TA-Lib SMA-seed. Divergence from TA-Lib: 0.41% at bar 60, decays exponentially.
- **[ADR-009](docs/adr/009-benchmark-methodology.md):** Four benchmark rules: (1) warm-path only, (2) realistic multi-ticker shape, (3) buffer-address-level zero-copy test, (4) mean-not-best SLA metric.

### 5. Week 2 Definition of Done (DoD)

- [x] CAF engine: `compute_caf_scalar`, `compute_caf_matrix`, `adjusted_close_as_of` — all 18 math tests passing.
- [x] Vectorized ASOF join engine: `join_features_as_of`, `compute_features_as_of` — 20 engine tests passing.
- [x] Declarative feature registry: DAG resolution, cycle detection, deterministic hash — 23 registry tests passing.
- [x] Technical feature views: `adj_close`, `momentum_20d`, `volatility_20d`, `sma_50d`, `ema_50d` — 9 feature tests, 5 integration tests passing.
- [x] Canary 01 Ancestor: `known_to` bitemporal restatement filtering verified in both unit and end-to-end integration tests.
- [x] Benchmark SLA: ASOF join < 100ms for 1,000–3,000 observations across multiple tickers — all scale levels PASS.
- [x] Zero-copy Arrow memory sharing verified at buffer-address level — no CSV/Parquet intermediaries.
- [x] `mypy --strict` passes with zero type errors across all 41 source files.
- [x] `ruff check .` and `ruff format --check .` both clean.
- [x] 131 unit tests passing, 0 failures, 0 errors.

---



