# Blueprint: Ledger
### Bitemporal Point-in-Time Feature Store & Leakage Canary Engine

**Domain:** Quantitative Data Engineering / Trading Infrastructure  
**Timeline:** 5 weeks (Core Build) + 1–2 weeks (SEC EDGAR Extension)  
**Execution:** Local-first, zero-infrastructure burden (DuckDB + Parquet)

---

## 1. Project Overview

### The 30-Second Summary
Backtests lie when trading strategies accidentally consume future information. A strategy that looks profitable in backtesting frequently collapses in live execution because of subtle look-ahead bugs: restated earnings, pre-adjusted split prices, weekend filing lags, or survivorship bias. 

**Ledger** is a point-in-time (PIT) feature store built for quantitative backtesting. It enforces bitemporal correctness by construction and proves it with an automated **Leakage Canary Suite** — a test suite of deliberately planted look-ahead defects that the pipeline is asserted to catch.

### Problem Statement
Most data pipelines model time as a single scalar `timestamp`. Financial data requires two distinct temporal dimensions:
1. **Valid Time (Market Reality):** When an event occurred in the market (e.g., quarter ended June 30, trade bar closed at 16:00:00).
2. **Transaction Time (Knowledge Time):** When that fact was recorded and became actionable to the trading system (e.g., 10-Q accepted on EDGAR at 17:30, trade bar consolidated post-auction at 16:15).

Querying data using valid time instead of transaction time introduces silent look-ahead bias. Ledger solves this by decoupling both dimensions into bitemporal intervals and exposing a vectorized ASOF join interface for historical strategy backtesting.

---

## 2. Core Architectural Principles

```
  ┌────────────────────────────────────────────────────────────────────────┐
  │                         DATA INGESTION                                 │
  │   - Raw Unadjusted Prices (EOD OHLCV)                                  │
  │   - Corporate Actions (Splits, Cash/Stock Dividends)                   │
  │   - SEC EDGAR Filings (10-K, 10-Q, 8-K, Restatements) [Stretch]        │
  └───────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │               TRUE APPEND-ONLY STORAGE (Partitioned Parquet)           │
  │   - Ingests store strictly `known_from` / `valid_from`                 │
  │   - Monotonic `ingestion_seq` guarantees deterministic window ordering │
  │   - No in-place row mutations across raw storage layers                │
  │   - Partitioned by: /year=YYYY/month=MM/                               │
  └───────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │               BITEMPORAL VIEW & CANONICALIZATION LAYER                 │
  │   • Derived Ticker Alias: [valid_from, LEAD(valid_from))               │
  │   • Derived Knowledge:    [known_from, LEAD(known_from))               │
  │   • Permanent SecID:      SecID <-> Ticker bitemporal alias map        │
  │   • Exchange Times:       NYSE/NASDAQ session cutoff & holiday rules   │
  └───────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │               VECTORIZED ASOF ENGINE (Polars / DuckDB)                 │
  │   join_features_as_of(observation_matrix, feature_views)               │
  │   - Joins on: known_from <= observation_timestamp < derived_known_to   │
  │   - Dynamic Cumulative Adjustment Factors (CAF) calculated as-of T_obs │
  └───────────────┬────────────────────────────────────────┬───────────────┘
                  │                                        │
                  ▼                                        ▼
  ┌───────────────────────────────┐        ┌───────────────────────────────┐
  │     LEAKAGE CANARY SUITE      │        │    VECTORIZED BACKTEST        │
  │  Automated pytest assertions  │        │  Runs Leaky vs. PIT pipelines │
  │  verifying defect classes     │        │  Compares Sharpe & drawdown   │
  └───────────────────────────────┘        └───────────────────────────────┘
```

1. **True Append-Only Ingestion with Derived Knowledge & Validity Intervals:** Raw storage files are strictly immutable and store only `known_from` (for transaction time) or `valid_from` (for entity mappings). The upper bounds `known_to` and `valid_to` are derived at query time using `LEAD()` window functions with deterministic `(timestamp, ingestion_seq)` ordering. No existing Parquet files or rows are ever mutated when restatements or symbol changes arrive.
2. **Selective Interval Versioning Rationale:** Only fundamental filings and entity mappings require versioned intervals (`LEAD()` derivation), since price bars and corporate actions are immutable per `(sec_id, trade_date)` and not subject to retroactive restatements in the same manner.
3. **Raw Unadjusted Storage with Dynamic Adjustment:** Prices are stored strictly unadjusted. Split and dividend Cumulative Adjustment Factors (CAF) are computed on-the-fly using only corporate actions where `known_from <= observation_timestamp`.
4. **Permanent Entity Resolution:** Securities are keyed by immutable `sec_id` (e.g., CIK or synthetic permanent ID). Ticker symbols are treated as time-varying attributes.
5. **Exchange Calendar Awareness:** Data availability respects exchange operating hours and settlement schedules (e.g., filings arriving Friday after 16:00 are not actionable until Monday 09:30 open).
6. **Observation Matrix ASOF Joins:** Features are served by performing vectorized ASOF joins against an input observation DataFrame ($N$ assets $\times$ $M$ timestamps).
7. **Content-Addressable Lineage:** Every backtest records a JSON manifest containing the environment hash, Git commit SHA, feature definition versions, and input Parquet SHA-256 hashes for exact replayability.

---

## 3. Technology Stack & Architectural Decisions

| Layer | Tool | Rationale |
|---|---|---|
| **Language** | Python 3.11+ | Modern typing, pattern matching, native performance. |
| **Validation & Schemas** | `pydantic` v2 | Strict schema validation for ingestion and feature configs. |
| **Storage Format** | Apache Parquet (Snappy) | Columnar, portable, Hive-partitioned (`/year=YYYY/month=MM/`). |
| **Query & Join Engine** | DuckDB & Polars | Vectorized execution, native ASOF join support, zero-copy Arrow memory transport. |
| **Exchange Calendar** | `exchange_calendars` | Precise NYSE/NASDAQ trading sessions, early closes, and holiday boundaries. |
| **Data Ingestion** | `yfinance` / Stooq / SEC EDGAR API | Publicly available historical price, split, and filing data. |
| **Testing Harness** | `pytest` | Automated execution of unit tests and the leakage canary suite. |
| **Backtest Consumer** | `vectorbt` / Vectorized NumPy | Fast proof-of-concept backtest comparing leaky vs. corrected feature inputs. |
| **Lineage** | Hash-verified JSON manifest | SHA-256 hashes of data artifacts, lockfiles, and git commit tracking. |
| **UI / Dashboard (Optional)** | Streamlit & Plotly | Optional visualization for PIT time-slider and performance comparison. |

### Architectural Decision: Hand-Rolled ASOF Engine vs. Feast
* **Context:** Feast is the standard open-source feature store for machine learning.
* **Decision:** Implement a focused bitemporal ASOF engine directly using DuckDB and Polars rather than deploying Feast.
* **Rationale:** Feast is primarily optimized for low-latency online key-value lookups (e.g., Redis) and ML inference. Historical backtesting requires interval-based bitemporal ASOF joins and dynamic corporate action adjustment, which are more cleanly and efficiently executed directly in columnar analytical engines (DuckDB/Polars). Building this directly demonstrates a fundamental understanding of temporal joins and time-series query planning.

---

## 4. Data Model & Schemas

### 4.1. Security Entity Mapping Raw & View
*Append-only raw table. `valid_to` is derived at query time to handle symbol drift (e.g. `FB` $\to$ `META`) without mutation.*
```sql
CREATE TABLE dim_security_ticker_raw (
    sec_id              VARCHAR NOT NULL,      -- Permanent Security ID
    ticker              VARCHAR NOT NULL,      -- Ticker symbol active from valid_from
    valid_from          TIMESTAMP NOT NULL,
    ingestion_seq       BIGINT NOT NULL,       -- Monotonic tiebreaker for concurrent writes
    PRIMARY KEY (sec_id, valid_from, ingestion_seq)
);

CREATE VIEW v_bitemporal_ticker_map AS
SELECT 
    sec_id,
    ticker,
    valid_from,
    LEAD(valid_from) OVER (
        PARTITION BY sec_id 
        ORDER BY valid_from, ingestion_seq
    ) AS valid_to
FROM dim_security_ticker_raw;
```

### 4.2. Raw Market Data (`fact_market_ohlcv_raw`)
*Append-only table. Immutable records per `(sec_id, trade_date)`.*
```sql
CREATE TABLE fact_market_ohlcv_raw (
    sec_id              VARCHAR NOT NULL,
    trade_date          DATE NOT NULL,
    open                DOUBLE NOT NULL,
    high                DOUBLE NOT NULL,
    low                 DOUBLE NOT NULL,
    close               DOUBLE NOT NULL,
    volume              BIGINT NOT NULL,
    known_from          TIMESTAMP NOT NULL,    -- Ingestion timestamp (post-market close)
    ingestion_seq       BIGINT NOT NULL
);
```

### 4.3. Corporate Actions (`fact_corporate_actions`)
```sql
CREATE TABLE fact_corporate_actions (
    sec_id              VARCHAR NOT NULL,
    action_type         VARCHAR NOT NULL,      -- 'SPLIT', 'CASH_DIVIDEND'
    ex_date             DATE NOT NULL,         -- Effective market date
    split_ratio         DOUBLE,                -- 4.0 for 4-for-1; 0.5 for 1-for-2 reverse
    cash_amount         DOUBLE,
    announcement_date   DATE NOT NULL,
    known_from          TIMESTAMP NOT NULL,    -- When publicly known to system
    ingestion_seq       BIGINT NOT NULL
);
```

### 4.4. Fundamental Filings (`fact_fundamentals_raw`) [Stretch]
```sql
CREATE TABLE fact_fundamentals_raw (
    sec_id              VARCHAR NOT NULL,
    filing_type         VARCHAR NOT NULL,      -- '10-Q', '10-K', '10-Q/A' (Amendment)
    fiscal_period_end   DATE NOT NULL,         -- Valid time
    metric_name         VARCHAR NOT NULL,      -- 'eps_diluted', 'total_revenue'
    metric_value        DOUBLE NOT NULL,
    acceptance_time     TIMESTAMP NOT NULL,    -- SEC EDGAR timestamp
    known_from          TIMESTAMP NOT NULL,    -- Actionable trading time (acceptance + calendar rules)
    is_restatement      BOOLEAN DEFAULT FALSE,
    supersedes_id       VARCHAR,
    ingestion_seq       BIGINT NOT NULL
);
```

### 4.5. Dynamic Bitemporal Query View (Derived `known_to`)
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

---

## 5. Vectorized PIT Retrieval Engine

The core retrieval function executes a bitemporal ASOF join against an input observation matrix (`entity_df` containing `[sec_id, observation_timestamp]`):

```python
def join_features_as_of(
    entity_df: pl.DataFrame,
    feature_views: list[str],
    as_of_column: str = "observation_timestamp"
) -> pl.DataFrame:
    """
    Performs a point-in-time correct bitemporal join.
    
    Invariants:
    1. Only returns data records where:
       known_from <= observation_timestamp AND (known_to IS NULL OR observation_timestamp < known_to)
    2. Dynamic split adjustments (CAF) only evaluate corporate actions where:
       known_from <= observation_timestamp
    3. Prevents look-ahead across all joined technical and fundamental features.
    """
```

### Dynamic Adjustment Math (Cumulative Adjustment Factor)
For any observation date $T_{obs}$ and historical price date $t \le T_{obs}$, the split adjustment factor is:
$$\text{CAF}(t, T_{obs}) = \prod_{k \in \mathcal{A}(t, T_{obs})} \text{split\_factor}_k$$
where $\mathcal{A}(t, T_{obs}) = \{ k \mid t < \text{ex\_date}_k \le T_{obs} \land \text{known\_from}_k \le T_{obs} \}$.

Any split occurring at $T > T_{obs}$ does not affect historical prices when querying at $T_{obs}$.

---

## 6. The Leakage Canary Suite Catalog

The canary suite (`tests/canaries/`) consists of programmatic test cases. Each canary seeds a synthetic scenario containing a specific leakage vector and asserts that the engine isolates or rejects it.

### Core Priority Tiering
- **Tier 1 (Non-Negotiable Core):** Canaries 01, 02, 03, 05 (Restatements, Splits, After-Hours, Filing Lag).
- **Tier 2 (Graceful Fallback if Schedule Compresses):** Canaries 04, 06 (Survivorship, Ticker Re-identification).

| # | Canary Name | Tier | Leakage Defect Mechanism | Verification / Assertion |
|---|---|---|---|---|
| **01** | **Restated Fundamentals** | 1 | Q2 EPS originally filed at \$1.00, restated to \$0.70 in Nov. | Querying as-of Oct returns \$1.00; querying as-of Dec returns \$0.70. |
| **02** | **Retroactive Split** | 1 | 4-for-1 split occurs in August 2020. | Pre-split (July 2020) price queried as-of July is unadjusted; CAF applied only post-effective date. |
| **03** | **After-Hours / Session** | 1 | Earnings released Friday at 17:00 ET. | Actionable timestamp is shifted to Monday 09:30 open. Cannot be used for Friday 15:59 rebalance. |
| **04** | **Survivorship Universe** | 2 | Delisted entity (e.g. bankrupt bank) removed from current ticker lists. | Querying universe as-of 2008 includes the delisted entity; querying as-of 2012 excludes it. |
| **05** | **Filing Lag Window** | 1 | Quarter ends March 31; 10-Q filed May 10. | Features for Q1 are unavailable between March 31 and May 10. |
| **06** | **Ticker Re-identification** | 2 | Asset rebrands from `OLD_TICK` to `NEW_TICK` in 2020. | Queries in 2018 resolve to `OLD_TICK` via `SecID` mapping without dropping history. |

---

## 7. Comparative Proof: Leaky vs. PIT Backtest

To demonstrate the empirical impact of look-ahead bias, Ledger includes a reference strategy (e.g., 20-day Momentum + Earnings Revision Rank) executed across two pipelines:

1. **Leaky Control Pipeline:** Uses standard unversioned data (pre-adjusted Yahoo Finance prices, same-day earnings availability, static modern universe).
2. **Ledger PIT Pipeline:** Uses Ledger's bitemporal ASOF engine with strict point-in-time constraints.

### Output Metrics Comparison
- **Sharpe Ratio Delta:** Compares the inflated leaky performance against the corrected baseline.
- **Drawdown Realism:** Shows drawdown periods masked by retroactive split adjustments and survivorship exclusion.
- **Execution Script:** `python -m ledger.backtest.run_comparison` produces a summary table and comparative metrics.

---

## 8. Lineage & Hash-Verified Run Manifests

Every feature extraction and backtest run generates a reproducible `manifest.json`:
```json
{
  "ledger_version": "0.1.0",
  "git_commit_sha": "a1b2c3d4e5f67890123456789abcdef01234567",
  "python_version": "3.11.8",
  "lockfile_hash": "sha256:d5b8...f9a1",
  "run_timestamp_utc": "2026-09-12T03:15:00Z",
  "feature_definitions": {
    "momentum_20d": "sha256:7f83b1657ff1fc53b92dc18148a1d65dfc2d4b1fa3d677284addd200126d9069",
    "pe_ratio_pit": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  },
  "input_dataset_hashes": {
    "fact_market_ohlcv_raw.parquet": "sha256:4b227777d4dd1fc61c6f884f48641d02b4d121d3fd328cb08b5531fcacdabf8a",
    "fact_corporate_actions.parquet": "sha256:ef2d127de37b942baad06145e54b0c619a1f22327b2ebbcfbec78f5564afe39d"
  }
}
```

---

## 9. Repository Structure

```
ledger/
├── .github/
│   └── workflows/
│       └── ci.yml                   # Runs Ruff, MyPy, and the Canary Suite
├── docs/
│   └── canary_catalog.md            # Detailed documentation of the canaries
├── ledger/
│   ├── core/
│   │   ├── bitemporal.py            # Interval math & window-based known_to derivation
│   │   ├── calendars.py             # Exchange calendar session handling
│   │   └── entity.py                # SecID <-> Ticker bitemporal resolver
│   ├── ingestion/
│   │   ├── market_data.py           # Unadjusted OHLCV ingestion
│   │   ├── corporate_actions.py     # Split/dividend ingestion
│   │   └── edgar_fundamentals.py    # SEC EDGAR parser [Stretch]
│   ├── storage/
│   │   ├── catalog.py               # DuckDB connection & catalog management
│   │   └── partitions.py            # Hive partitioning read/write helpers
│   ├── features/
│   │   ├── registry.py              # Feature definitions & metadata
│   │   ├── definitions/
│   │   │   ├── technical.py         # Dynamic CAF-adjusted Momentum, Volatility
│   │   │   └── fundamentals.py      # PIT Valuation features [Stretch]
│   │   └── engine.py                # join_features_as_of vectorized ASOF engine
│   ├── lineage/
│   │   └── manifest.py              # Hash-verified manifest logger
│   └── backtest/
│       ├── strategy.py              # Reference momentum strategy
│       └── run_comparison.py        # Leaky vs. Corrected comparison runner
├── tests/
│   ├── canaries/                    # THE LEAKAGE CANARY SUITE (Core Deliverable)
│   │   ├── test_canary_01_restatements.py
│   │   ├── test_canary_02_retroactive_splits.py
│   │   ├── test_canary_03_after_hours_session.py
│   │   ├── test_canary_04_survivorship_universe.py
│   │   ├── test_canary_05_filing_lag_window.py
│   │   ├── test_canary_06_ticker_relabeling.py
│   │   └── conftest.py              # Synthetic bitemporal test fixtures
│   └── unit/
│       ├── test_asof_engine.py
│       └── test_caf_math.py
├── app/                             # [Optional / Phase 5 Polish]
│   └── streamlit_app.py             # Interactive PIT Inspector & Tear-Sheet
├── pyproject.toml
└── README.md
```

---

## 10. Known Limitations & Edge Cases

Documenting boundaries demonstrates engineering maturity:
1. **Ticker Reuse:** Exchanges occasionally reassign a delisted ticker symbol to a new, unrelated company after a multi-year gap. While Ledger's `SecID` architecture isolates internal storage, queries specifying only a raw ticker symbol across disjoint multi-decade spans must be resolved via date-scoped alias lookups.
2. **Intraday Bar Restatements:** Ledger models daily EOD bars and fundamental filings. Tick-level order book consolidation and trade bust cancellations are out of scope for the lean build.

---

## 11. Phased Implementation Roadmap

```
  ┌────────────────────────────────────────────────────────────────────────┐
  │ Weeks 1–4: Non-Negotiable Core (The Technical Thesis)                  │
  │ • Phase 1: Ingestion & Append-Only Bitemporal Storage Layer            │
  │ • Phase 2: Vectorized ASOF Engine & Dynamic CAF Adjustment             │
  │ • Phase 3: The Leakage Canary Suite (Tier 1 & Tier 2)                  │
  │ • Phase 4: Leaky vs. PIT Backtest Comparison & Manifest                │
  └───────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │ Week 5: Presentation & Polish (Time-Permitting)                        │
  │ • Clean README, architecture diagrams, CI workflow                     │
  │ • (Optional) Streamlit PIT visual inspector & ADRs                     │
  └───────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │ Weeks 6–7: Stretch Goal (SEC EDGAR Real Restatements)                  │
  │ • Ingest real 10-Q/10-K filings with SEC EDGAR acceptance timestamps   │
  │ • Test Canary 01 against real restatements (e.g. 10-Q/A amendments)    │
  └────────────────────────────────────────────────────────────────────────┘
```

### Phase 1: Bitemporal Foundations & Ingestion (Week 1)
- [ ] Set up `pyproject.toml` with Ruff, MyPy, Polars, DuckDB, `exchange_calendars`.
- [ ] Implement `dim_security_ticker_raw` and `v_bitemporal_ticker_map` (SecID $\leftrightarrow$ ticker).
- [ ] Ingest raw unadjusted OHLCV and corporate actions to append-only partitioned Parquet.

### Phase 2: Vectorized ASOF Engine & Dynamic Adjustment (Week 2)
- [ ] Implement Cumulative Adjustment Factor (CAF) dynamic calculation.
- [ ] Build `join_features_as_of()` using Polars / DuckDB ASOF join with derived `known_to`.
- [ ] Implement basic technical features (Momentum, Rolling Volatility).

### Phase 3: Leakage Canary Suite (Week 3 — Core Priority)
- [ ] Construct synthetic bitemporal test fixtures in `conftest.py`.
- [ ] Implement Tier 1 canaries (01, 02, 03, 05).
- [ ] Implement Tier 2 canaries (04, 06) if on schedule.
- [ ] Ensure full test suite passes deterministically via `pytest`.

### Phase 4: Comparative Backtest & Lineage Manifest (Week 4)
- [ ] Build `run_comparison.py` running strategy on leaky vs. corrected features.
- [ ] Implement `manifest.py` generating environment, lockfile, and data SHA-256 hashes.
- [ ] Verify comparative metrics (Sharpe, Drawdown) demonstrate the leakage impact.

### Phase 5: Documentation & Polish (Week 5)
- [ ] Write concise README with the 30-second elevator pitch and usage commands.
- [ ] Set up GitHub Actions CI workflow to run tests on every commit.
- [ ] *(Optional / Time-Permitting)* Build Streamlit PIT inspector and write ADRs.

### Stretch Goal: SEC EDGAR Bitemporal Restatements (Weeks 6–7)
- [ ] Ingest real 10-Q/10-K filings with EDGAR acceptance timestamps.
- [ ] Ingest 10-Q/A restatements and validate Canary 01 on live historical filings.

---

## 12. Interview Preparation & Technical Rationale

### Key Technical Questions & Answers
1. **Why not just use Feast or Tecton?**  
   *"Feast is designed for low-latency online KV serving (e.g., Redis for real-time model inference). Backtesting requires historical bitemporal ASOF interval joins across time-series and dynamic corporate action factor calculation, which is more efficiently and cleanly executed directly in columnar analytical engines like DuckDB and Polars."*

2. **Why store raw unadjusted prices instead of adjusted prices?**  
   *"Pre-adjusted prices retroactively scale all historical prices by future split/dividend factors, introducing look-ahead bias prior to the split announcement date. Ledger stores raw prices and applies a Cumulative Adjustment Factor (CAF) dynamically, using only corporate actions known as of the observation timestamp."*

3. **How do you keep storage append-only when restatements or symbol changes arrive?**  
   *"We store only `known_from` (for facts) and `valid_from` (for entity mappings) on raw records and never mutate existing Parquet files. Upper bounds are derived dynamically at query time using `LEAD() OVER (PARTITION BY ... ORDER BY timestamp, ingestion_seq)`. When a restatement or symbol change arrives, it is simply appended as a new row with its own sequence number."*

4. **Why don't price bars have versioned knowledge intervals?**  
   *"Only fundamentals and entity maps require versioned intervals, because historical price bars and corporate action logs are immutable per `(sec_id, trade_date)` and not subject to the statutory amendments/restatements typical of financial filings."*

5. **What is the difference between Valid Time and Transaction Time?**  
   *"Valid time is when the event occurred in market reality (e.g., fiscal quarter end on June 30). Transaction time is when our system received and processed the data (e.g., 10-Q filing arriving August 8 at 17:30). Strategies can only make decisions based on transaction time."*

6. **How does the canary suite prove the architecture works?**  
   *"Compilers use regression suites; Ledger uses leakage canaries. We deliberately inject temporal defects (restatements, future splits, after-hours filings, delistings, filing lags, symbol renames) and assert that the feature engine isolates or catches them before feature delivery."*
