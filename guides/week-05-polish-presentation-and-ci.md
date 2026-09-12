# Week 5 Guide: Presentation, Production Documentation & CI/CD

**Primary Objective:** Deliver a portfolio presentation that immediately signals senior-level engineering maturity to recruiters and hiring managers. Set up automated GitHub Actions CI, write concise technical documentation and Architecture Decision Records (ADRs), and optionally build the interactive Streamlit Leakage Inspector.

---

## 1. Definition of Done (DoD) for Week 5
- [ ] GitHub Actions CI workflow operational (`.github/workflows/ci.yml`) running linting (`ruff`), strict type-checking (`mypy`), and the entire Leakage Canary Suite.
- [ ] High-impact `README.md` completed with the 30-second elevator pitch, architecture diagram, and reproducible CLI instructions.
- [ ] 3 Architecture Decision Records (ADRs) written in `docs/adr/`.
- [ ] Standalone `docs/canary_catalog.md` finalized with defect diagrams and assertions.
- [ ] (Optional / Time-Permitting) Streamlit "Leakage Inspector" app built in `app/streamlit_app.py`.
- [ ] Repository clean: zero lint warnings, 100% type annotations, clean `.gitignore`, clear commit history.

---

## 2. Day-by-Day Implementation Plan

### Day 1: GitHub Actions CI & Quality Automation
1. **Create `.github/workflows/ci.yml`:**
   - Steps:
     1. Checkout repo with full git history.
     2. Set up Python 3.11 with caching.
     3. Install dependencies (`uv sync` / `poetry install`).
     4. Run Ruff format and lint checks (`ruff check .`, `ruff format --check .`).
     5. Run MyPy in strict mode (`mypy .`).
     6. Run Pytest unit and canary suites (`pytest -v --cov=ledger`).
2. **Add CI Status Badges to README:**
   - `[![CI](https://github.com/username/ledger/actions/workflows/ci.yml/badge.svg)]`
   - `[![Type-Checked: mypy strict](https://img.shields.io/badge/types-mypy%20strict-blue.svg)]`
   - `[![Canary Suite: 6/6 Passed](https://img.shields.io/badge/canaries-6%2F6%20passed-brightgreen.svg)]`

### Day 2: Executive README & Visual Artifacts
1. **Structure `README.md`:**
   - **Section 1 (0–30s):** Plain-English problem summary + elevator pitch ("Why backtests lie").
   - **Section 2:** Architecture Diagram (ASCII or Mermaid) showing bitemporal intervals and vectorized ASOF joins.
   - **Section 3:** Quickstart (3 commands to clone, run canaries, and reproduce the comparison).
   - **Section 4:** The Comparative Tear-Sheet (ASCII table comparing Leaky vs. PIT Sharpe, Return, Drawdown).
   - **Section 5:** The Canary Catalog summary table.
   - **Section 6:** Known Limitations & Edge Cases (ticker reuse, daily bar scope).

### Day 3: Architecture Decision Records (ADRs)
1. **Write `docs/adr/0001-bitemporal-interval-model.md`:**
   - Explains Valid Time vs. Transaction Time, append-only raw storage, and why `known_to` is derived via `LEAD()` rather than stored.
2. **Write `docs/adr/0002-duckdb-polars-asof-engine.md`:**
   - Compares Feast vs. custom DuckDB/Polars ASOF engine for historical backtesting.
3. **Write `docs/adr/0003-hash-verified-manifest.md`:**
   - Explains the content-addressable SHA-256 data snapshot, lockfile, and Git SHA tracking for deterministic backtest replay.

### Day 4: Optional Polish — Streamlit Leakage Inspector (`app/streamlit_app.py`)
*(Time-permitting presentation layer)*
1. **Interactive Time-Slider:**
   - Allow user to drag an $as\_of\_date$ slider across a stock split (e.g. AAPL August 2020) and observe the raw unadjusted price vs. the dynamically adjusted feature value.
2. **Side-by-Side Equity Curve Chart:**
   - Plot Plotly interactive chart showing the Leaky strategy equity curve vs. the Ledger PIT equity curve.

### Day 5: Final Repo Polish & Technical Interview Rehearsal
1. **Repository Hygiene:**
   - Remove temporary scratch files, check `.gitignore` ignores `/data/*.parquet` and cache directories.
   - Run a clean clone test in a fresh virtual environment:
     ```bash
     git clone <repo>
     pip install -e ".[dev]"
     pytest -v tests/canaries/
     python -m ledger.backtest.run_comparison
     ```
2. **Rehearse Technical Interview Questions:**
   - Practice the 4 core interview defenses (Bitemporality, Dynamic CAF, ASOF joins, Canary assertions).

---

## 3. Key Presentation Traps to Avoid in Week 5
- **Trap 1:** Burying the thesis under complex setup instructions. Make sure any reviewer can run `pytest tests/canaries/` in under 60 seconds.
- **Trap 2:** Making unsubstantiated benchmark claims. Base all README numbers and resume bullets strictly on the actual test and backtest runs produced by your code.
- **Trap 3:** Letting optional UI work delay shipping a clean, bug-free codebase with green CI.
