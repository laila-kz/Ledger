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


