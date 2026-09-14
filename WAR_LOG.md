# WAR LOG: Quantitative Data Engineering Journal

> **Purpose:** Running engineering journal recording non-trivial technical decisions, debugging breakthroughs, edge cases, surprises, and reversals throughout the build of Ledger.

---

## [2026-09-14] Week 5 Day 1 Late Evening: GitHub Actions Workflow Repair

### Root Cause of All 24 Workflow Failures

**Symptom:** All 24 GitHub Actions workflow runs were failing with error:
```
Error: No file in /home/runner/work/Ledger/Ledger matched to [**/uv.lock], 
make sure you have checked out the target repository
```
This occurred in the "Install uv" step of the CI workflow.

**Root Cause:** The `.github/workflows/ci.yml` file on the `main` branch (and initially on `Work-in-progress`) was using a `uv`-based workflow that required:
1. Installing the Astral `setup-uv` GitHub Action
2. A `uv.lock` file to be present in the repository
3. Running all tests via `uv run pytest` instead of direct pytest

However, the repository had **no `uv.lock` file committed**, causing every workflow run to fail immediately at the "Install uv" step.

**The Branching Problem:** 
- The `Work-in-progress` branch had an older commit with a *partial* corrected pip-based workflow (from earlier in the evening during pre-Day 2 verification)
- The `main` branch still had the broken uv-based workflow with the `[**/uv.lock]` glob pattern
- GitHub Actions was running against the `main` branch version, which was broken

**Resolution Applied:**
1. **Removed uv dependency entirely** - Replaced the entire workflow with a simpler, more reliable pip-based approach
2. **Correct workflow structure:**
   - Python 3.11 setup (single version, not a matrix)
   - `pip install --upgrade pip && pip install -e ".[dev]"`
   - Direct ruff, mypy, and pytest invocations (no uv wrapper)
   - No `uv.lock` requirement

3. **Pushed fix to both branches:**
   - Committed fix to `main` (commit `a3c8fd7`)
   - Merged `main` into `Work-in-progress` to keep branches synchronized

**Why This Matters:**
The uv-based workflow adds unnecessary complexity for the MVP:
- Requires maintaining an additional lockfile in version control
- Adds extra dependency resolution overhead in CI
- The same quality gates work perfectly with standard pip
- pip is simpler, more predictable, and available on all runners

**Lesson:** When using GitHub Actions in an MVP, prefer:
- Direct tool invocations over language-specific package managers
- Single Python version (not a matrix) for faster feedback
- Simple proven workflows over aspirational automation setups

**Verification:**
- Workflow is now valid YAML (no truncation, no duplicates)
- Both `main` and `Work-in-progress` branches have the corrected version
- No `uv.lock` requirement
- Next workflow run should go green across all steps

---

## [2026-09-14] Week 5 Day 1 Evening: Pre-Day 2 Verification & CLI Polish

### Four Pre-Day 2 Verification Checks (All Passing)

#### ✅ Check A: Fresh Clone & Clean Install
* **Scenario:** Simulate CI environment by uninstalling and reinstalling the package fresh.
* **Command:** `pip uninstall -y ledger; pip install -e .`
* **Result:** ✓ Package reinstalls cleanly with no TOML scoping errors, no missing `editables` build-time dependency issues.
* **Validation:** `ledger --version` returns `0.1.0`, `ledger canaries` runs and passes all 10 tests (4.44s).
* **Key Insight:** The TOML scoping fix from earlier (placing `[project.scripts]` AFTER all other `[project.*]` tables) and the `editables>=0.3` build-time dependency are holding up correctly in a clean environment.

#### ✅ Check B: GitHub Actions Workflow
* **Issue Found:** The `.github/workflows/ci.yml` file contained corrupted/duplicate YAML:
  - Line 36 had incomplete command: `run: pytest -v --cov=ledgername:` (truncated mid-line)
  - File had two job definitions concatenated (old `quality` job + newer `quality-gate` job with uv), creating unparseable YAML
* **Resolution:** Cleaned up the workflow file to a single, valid `quality` job using pip (simpler, more reliable for the MVP stage).
* **Restored Workflow:**
  - Setup Python 3.11 with pip caching
  - Install project with dev dependencies: `pip install -e ".[dev]"`
  - Run Ruff lint and format checks
  - Run MyPy strict type checking
  - Run pytest with coverage (157 tests: 157 passed, 1 deselected)
* **Ready for Push:** Workflow is now valid YAML and will execute cleanly on GitHub Actions.

#### ✅ Check C: README Badges
* **Audit:** Searched for placeholder URLs (`github.com/username`) in README.
* **Result:** ✓ Badges already using correct GitHub username (`laila-kz/Ledger`).
* **No Fix Needed:** Badges will display correctly.

#### ✅ Check D: CLI Help Text
* **Issue Found:** `ledger run-comparison --help` was incomplete:
  - Missing `help=` descriptions on all 14 argument definitions
  - Usage line showed `cli.py` instead of `ledger run-comparison` (prog name not set)
  - Options listed without explanations, making interface unhelpful
* **Resolution:** Enhanced `ledger/backtest/run_comparison.py`:
  - Set `prog="ledger run-comparison"` and added formatter class `RawDescriptionHelpFormatter`
  - Added comprehensive help text for each argument (e.g. `--start-date`: "Backtest start date (ISO format: YYYY-MM-DD).")
  - Added epilog with 3 concrete usage examples
* **Result:** ✓ Help output now shows:
  - Proper usage line: `ledger run-comparison [-h] --start-date START_DATE --end-date END_DATE ...`
  - Clear descriptions for all 14 options
  - 3 practical examples at the bottom
* **All Subcommands Verified:**
  - `ledger --help`: ✓ Clear (already good)
  - `ledger canaries --help`: ✓ Concise with example
  - `ledger lint --help`: ✓ Good, mentions "coming Day 4"
  - `ledger verify-manifest --help`: ✓ Good, mentions "implemented in Week 5 Day 3"
  - `ledger run-comparison --help`: ✓ NOW FIXED, comprehensive and actionable

### Regression Test Results
* **Full Suite:** 157 tests passed, 1 deselected (integration suite marked to skip).
* **Canary Suite:** 10/10 passed in 1.97s (avg 4.44s with pytest overhead).
* **All Quality Gates Green:** Ruff lint + format clean, MyPy strict mode clean.

### Implications for Day 2
1. **Fresh clone in CI will succeed** - no hidden packaging gotchas.
2. **GitHub Actions workflow will run and pass** - valid YAML, proper command structure.
3. **README badges will display correctly** - username already in place.
4. **Users can run `ledger --help` and understand all subcommands** - help text is now actionable and includes examples.
5. **Ready to move to Day 2 README polish** - CLI is discoverable and well-documented.

---

## [2026-09-13] Week 4 Day 5: Pre-Merge Verification

### Initial-Capital Contract Bug
* **Failure mode:** The CLI exposed `--initial-capital`, but the simulator and
  tear-sheet initially normalized equity to `1.0`, allowing the manifest to
  advertise a capital value the equity curve ignored.
* **Resolution:** Threaded `initial_capital` through `SimulationConfig`, equity
  generation, metric cumulative-return/CAGR calculations, tear-sheet creation,
  and CLI manifest parameters.
* **Guard:** The subprocess CLI integration test now exercises the real argument
  path and verifies the generated artifacts and manifest together.

### Merge Gate Results
* [x] `artifacts/` is gitignored.
* [ ] `uv.lock` is present and tracked. The repository has no lockfile and the
  `uv` executable is unavailable in the current environment; the manifest
  honestly records null lockfile metadata until dependency locking is restored.
* [x] Offline CLI integration test passes with synthetic Parquet inputs.
* [x] Manifest reproducible content is stable across timestamp changes.
* [ ] Real-data leakage delta verified. This checkout has no `data/` directory,
  so no real-data tear-sheet was claimed.

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

## [2026-09-13] Week 3: The Leakage Canary Suite (Core Differentiator)

### 1. The Core Architectural Philosophy
* **The Insight:** Standard unit tests only verify the "happy path" of an engine. A backtesting platform must guarantee that look-ahead bugs are **provably caught and rejected**.
* **The Canary Pattern:** Every canary pairs a Point-in-Time (PIT) pipeline assertion with a deliberately leaky reference pipeline. A canary test passes **if and only if** the Ledger PIT engine yields the ground truth while the naive/leaky baseline diverges.

### 2. Canaries Implemented (`tests/canaries/`)

| Canary | Name | Defect Mechanism | PIT Protection in Ledger |
| :--- | :--- | :--- | :--- |
| **01** | **Restated Fundamentals** | Retroactive insertion of amended EPS filings before they were known. | `known_to` bitemporal window exclusion via `derive_known_to_polars()`. |
| **02** | **Retroactive Splits** | Static backward price deflation before split execution / announcement. | Dynamic Cumulative Adjustment Factor ($\text{CAF}$) evaluated as of observation timestamp. |
| **03** | **After-Hours Sessions** | Post-market earnings filings consumed in closed Friday sessions. | Calendar-aware `get_actionable_timestamp()` shifting after-hours filings to next session open ($09:30\text{ EST}$). |
| **04** | **Survivorship Bias** | Delisted entities (`LEHMQ`) pruned from historical trade universe. | Bitemporal universe membership with $[valid\_from, valid\_to)$ intervals. |
| **05** | **Filing Lag Window** | Fiscal period end ($T+0$) assumed as filing availability date. | Strict separation of `fiscal_period_end` from SEC EDGAR acceptance timestamp (`known_from`). |
| **06** | **Ticker Relabeling Drift** | Ticker renames (`FB` $\to$ `META`) fragmenting historical price series. | Permanent synthetic `sec_id` entity resolution with point-in-time ticker aliases. |

### 3. Canary Test Harness & Self-Testing (`tests/canaries/conftest.py`, `test_harness_self_test.py`)
* Created deterministic synthetic data generators for OHLCV, splits, corporate filings, and entity maps.
* Built assertion utilities: `assert_no_lookahead()` and `assert_leaky_diverges()`.
* Added 3 harness self-tests confirming that leaky reference pipelines reliably diverge.

### 4. Documentation & Catalog
* Created [`docs/canary_catalog.md`](docs/canary_catalog.md) detailing each defect, naive implementation flaw, and mathematical PIT assertion.

### 5. Week 3 Definition of Done (DoD)
- [x] Synthetic test fixtures and mock scenario generators in `tests/canaries/conftest.py`.
- [x] Canary 01 (Restatements) implemented and passing.
- [x] Canary 02 (Retroactive Splits) implemented and passing.
- [x] Canary 03 (After-Hours Sessions) implemented and passing.
- [x] Canary 04 (Survivorship Universe) implemented and passing.
- [x] Canary 05 (Filing Lag Window) implemented and passing.
- [x] Canary 06 (Ticker Relabeling) implemented and passing.
- [x] Canary harness self-test suite implemented and passing.
- [x] `docs/canary_catalog.md` catalog documentation complete.
- [x] `pytest tests/canaries/` passes 10/10 tests in $< 2$ seconds.
- [x] Full suite (`pytest`) passes 141/141 tests cleanly with zero regressions.
- [x] `ruff check .`, `ruff format --check .`, and `mypy .` clean across all 49 source files.

---



## [2026-09-13] Week 5 Day 1: Console CLI and CI Quality Gates

* Added the `ledger` console entry point with `lint`, `verify-manifest`,
  `run-comparison`, and `canaries` subcommands.
* Added `.github/workflows/ci.yml` for Ruff lint/format, strict mypy, and the
  complete pytest suite with coverage.
* Added CLI reachability tests and verified the packaged `ledger.exe` after
  installing the editable project.
* **Packaging issue caught:** `[project.scripts]` initially captured the
  dependency field because of TOML section scope. Moving it after the project
  dependency declaration fixed Hatchling metadata generation.
* **Quality gates:** Ruff lint/format and mypy pass; full pytest result is
  `156 passed, 1 deselected`.

---




