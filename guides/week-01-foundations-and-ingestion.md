# Week 1 Guide: Bitemporal Foundations, Schemas & Append-Only Ingestion

**Primary Objective:** Set up the project foundation, implement the mathematical bitemporal interval logic with derived bounds (`known_to` and `valid_to`), establish the immutable DuckDB/Parquet storage layer, and ingest raw market data and corporate actions.

---

## 1. Definition of Done (DoD) for Week 1
- [ ] Development environment configured with Python 3.11+, `uv` or `poetry`, Ruff, and MyPy (strict mode).
- [ ] Bitemporal interval math module implemented and unit-tested (`ledger/core/bitemporal.py`).
- [ ] Exchange calendar wrapper operational for NYSE market hours (`ledger/core/calendars.py`).
- [ ] Permanent `SecID` $\leftrightarrow$ `ticker` bitemporal resolver implemented with derived `valid_to` view (`ledger/core/entity.py`).
- [ ] Parquet partition write/read utilities and DuckDB catalog interface built (`ledger/storage/`).
- [ ] Raw unadjusted OHLCV and corporate actions ingestion pipelines built and verified (`ledger/ingestion/`).
- [ ] All Week 1 unit tests passing with 100% type-check clean (`mypy --strict`).

---

## 2. Day-by-Day Implementation Plan

### Day 1: Project Scaffolding & Tooling
1. **Initialize Project & Dependencies:**
   Create `pyproject.toml` with:
   - Core: `polars`, `duckdb`, `pyarrow`, `pydantic>=2.0`, `exchange_calendars`, `yfinance`
   - Backtesting: `numpy`, `scipy`, `vectorbt` (or pure numpy vectorized routines)
   - Dev & Test: `pytest`, `pytest-cov`, `ruff`, `mypy`
2. **Configure Strict Quality Gates:**
   Configure `tool.ruff` (linting & formatting) and `tool.mypy` (`strict = true`, `disallow_untyped_defs = true`).

### Day 2: Bitemporal Core & Interval Derivation
1. **Implement `ledger/core/bitemporal.py`:**
   - Define immutable Pydantic models / dataclasses for `ValidInterval` ($[valid\_from, valid\_to)$) and `TransactionInterval` ($[known\_from, known\_to)$).
   - Implement window-based SQL derivation helpers for DuckDB and Polars:
     ```sql
     LEAD(known_from) OVER (
         PARTITION BY sec_id, metric_name, fiscal_period_end 
         ORDER BY known_from, ingestion_seq
     ) AS known_to
     ```
2. **Implement `ledger/core/calendars.py`:**
   - Wrap `exchange_calendars` for `XNYS` (NYSE).
   - Implement `get_actionable_timestamp(event_time, is_market_data)`:
     - EOD bars at 16:00 EST $\to$ actionable at 16:15 EST.
     - Filings after 16:00 EST or on weekends $\to$ actionable at next trading session 09:30 EST open.

### Day 3: Entity Resolution & Permanent Identifiers
1. **Implement `ledger/core/entity.py`:**
   - Model `dim_security_ticker_raw` with `sec_id`, `ticker`, `valid_from`, `ingestion_seq`.
   - Build query function `resolve_sec_id(ticker, as_of_date)` using derived `valid_to` view:
     ```sql
     SELECT sec_id FROM v_bitemporal_ticker_map
     WHERE ticker = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)
     ```
2. **Write Unit Tests in `tests/unit/test_entity.py`:**
   - Test ticker rename scenario (e.g. `FB` $\to$ `META` on 2022-06-09).
   - Assert `resolve_sec_id('FB', '2020-01-01') == resolve_sec_id('META', '2023-01-01')`.

### Day 4: Storage Layer & Parquet Partitioning
1. **Implement `ledger/storage/catalog.py` & `partitions.py`:**
   - Manage DuckDB in-memory or persistent connections reading directly from partitioned Parquet files.
   - Standardize directory layout:
     `data/raw/market_ohlcv/year=YYYY/month=MM/*.parquet`
     `data/raw/corporate_actions/year=YYYY/*.parquet`
     `data/raw/entity_map/*.parquet`
   - Enforce append-only writes: new batches generate new Parquet files with incremented `ingestion_seq` (no overwrite of existing files).

### Day 5: Ingestion Pipelines & End-of-Week Verification
1. **Implement `ledger/ingestion/market_data.py` & `corporate_actions.py`:**
   - Ingest 5–10 representative liquid US tickers (e.g. `AAPL`, `MSFT`, `NVDA`, `META`, `GOOGL`) plus corporate action test cases (e.g. `AAPL` 4:1 split on 2020-08-31, `TSLA` 5:1 split on 2020-08-31).
   - Verify prices stored are **strictly unadjusted**.
2. **Run Validation Suite:**
   - Execute `pytest tests/unit/`.
   - Run `ruff check .` and `mypy .`.

---

## 3. Key Architectural Traps to Avoid in Week 1
- **Trap 1:** Storing pre-adjusted prices from `yfinance`. Always request unadjusted OHLCV and separate split/dividend history.
- **Trap 2:** Writing `known_to` or `valid_to` as physical columns in Parquet files. Parquet files are immutable; store only `known_from` / `valid_from` + `ingestion_seq`, and derive upper bounds in views.
- **Trap 3:** Continuous-time assumptions. Ignoring weekend/holiday calendars will break Canary 03 in Week 3.
