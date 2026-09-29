# Week 5 Guide: Presentation, Production Documentation, CI/CD & Developer Tooling

**Primary Objective:** Deliver a portfolio presentation and developer tooling suite that immediately signals senior-level infrastructure and quant systems maturity to recruiters and hiring managers. Set up automated GitHub Actions CI with console script CLI entrypoints (`ledger`), write concise technical documentation and 4 Architecture Decision Records (ADRs), implement a Static AST Leakage Linter (`ledger lint`), polish hash manifest verification (`ledger verify-manifest`), and assemble the final interview defense.

---

## 1. Definition of Done (DoD) for Week 5
- [ ] Console-script entrypoint configured in `pyproject.toml` (`ledger = "ledger.cli:main"`) exposing `ledger lint`, `ledger verify-manifest`, `ledger run-comparison`, and `ledger canaries`.
- [ ] GitHub Actions CI workflow operational (`.github/workflows/ci.yml`) running linting (`ruff`), strict type-checking (`mypy`), and the entire Leakage Canary Suite.
- [ ] High-impact `README.md` completed with the 30-second elevator pitch, architecture diagram, and reproducible CLI instructions.
- [ ] Standalone `docs/canary_catalog.md` finalized with defect diagrams, mathematical definitions, and assertions.
- [ ] Hash Manifest Verifier (`ledger/lineage/manifest.py` & `ledger verify-manifest`) recording SHA-256 digests of every input so drift is detectable.
- [ ] Static AST "Leakage Linter" (`ledger/tools/linter.py` & `ledger lint`) detecting lookahead antipatterns in user alpha scripts.
- [ ] Comprehensive unit tests for AST Linter and Manifest Verifier covering all 4 detection rules with positive (leaky) and negative (compliant) test fixtures.
- [ ] 4 Architecture Decision Records (ADRs) written in `docs/adr/` capturing all foundational systems decisions.
- [ ] Repository clean: zero lint warnings, 100% type annotations, clean `.gitignore`, clear commit history.

---

## 2. Day-by-Day Implementation Plan

### Day 1: GitHub Actions CI, Quality Automation & CLI Console Entrypoint
1. **Configure CLI Entrypoint in `pyproject.toml` & `ledger/cli.py`:**
   - Add `[project.scripts]` section mapping `ledger = "ledger.cli:main"`.
   - Wire subcommands (`ledger lint`, `ledger verify-manifest`, `ledger run-comparison`, `ledger canaries`) so all README commands run cleanly out-of-the-box.
2. **Create `.github/workflows/ci.yml`:**
   - Steps:
     1. Checkout repo with full git history.
     2. Set up Python 3.11 with caching.
     3. Install dependencies (`pip install -e .` / `uv sync`).
     4. Run Ruff format and lint checks (`ruff check .`, `ruff format --check .`).
     5. Run MyPy in strict mode (`mypy .`).
     6. Run Pytest unit and canary suites (`pytest -v --cov=ledger`).
3. **Add CI Status Badges to README:**
   - `[![CI](https://github.com/username/ledger/actions/workflows/ci.yml/badge.svg)]`
   - `[![Type-Checked: mypy strict](https://img.shields.io/badge/types-mypy%20strict-blue.svg)]`
   - `[![Canary Suite: 6/6 Passed](https://img.shields.io/badge/canaries-6%2F6%20passed-brightgreen.svg)]`

### Day 2: Executive README & Visual Artifacts
1. **Structure `README.md`:**
   - **Section 1 (0–30s):** Plain-English problem summary + elevator pitch ("Why backtests lie: lookahead bias, restatements, survivorship bias").
   - **Section 2:** Architecture Diagram (ASCII or Mermaid) showing bitemporal intervals, dynamic CAF chains, and vectorized ASOF joins.
   - **Section 3:** Quickstart (3 verified commands to clone, run canaries, and reproduce the comparison).
   - **Section 4:** The Comparative Tear-Sheet (ASCII table comparing Leaky vs. PIT Sharpe, Return, Drawdown).
   - **Section 5:** The Canary Catalog summary table.
   - **Section 6:** Static AST Linter & Hash Verification CLI tooling overview.
   - **Section 7:** Known Limitations & Edge Cases (ticker reuse, daily bar scope).

### Day 3: Standalone Canary Catalog & Hash Manifest Verifier Polish
1. **Finalize `docs/canary_catalog.md`:**
   - Create standalone catalog documenting all 6 leakage modes with ASCII timeline diagrams, mathematical failure mechanisms, and code snippets.
2. **Polish Hash Manifest Verifier (`ledger/lineage/manifest.py` & `ledger verify-manifest`):**
   - Implement standalone verification logic that re-hashes input Parquet partitions and feature DAG definitions, so a changed input, refactored feature, or shifted dependency shows up as a mismatch.
   - Add verification test cases in `tests/unit/test_manifest.py`.

### Day 4: Static AST "Leakage Linter" for Quant Alpha Code (`ledger/tools/linter.py`)
1. **Build Python AST Code Analyzer:**
   - Parse alpha scripts into Python Abstract Syntax Trees (`ast.parse`).
   - Implement `NodeVisitor` classes detecting:
     - **Rule 1 (Negative Shifts):** Calling `.shift(-k)` or negative index slices on time series (`df['close'].shift(-1)`).
     - **Rule 2 (Full-Sample Normalization):** Calling global aggregations (`.mean()`, `.std()`, `StandardScaler.fit()`) on un-windowed time series.
     - **Rule 3 (Unbounded Forward Fill):** Unconstrained `.ffill()` across publication boundaries.
     - **Rule 4 (Unsanitized Join Keys):** Direct joins on filing dates without point-in-time publication lag bounds.
2. **Expose CLI & Rich Terminal Output:**
   - Provide `ledger lint <script.py>` displaying file, line number, offending code snippet, and actionable mitigation advice.
3. **Write Unit Tests (`tests/unit/test_linter.py`):**
   - Test detection across all 4 rules with paired positive (leaky) and negative (compliant) test fixtures.

### Day 5: 4 Architecture Decision Records (ADRs), Repo Polish & Interview Rehearsal
1. **Write the 4 Core ADRs in `docs/adr/` (written post-implementation for maximum technical precision).** These were originally filed as `0001`–`0004` and have since been renumbered to continue the main series; the current filenames are:
   - **`017-bitemporal-interval-model.md`:** Explains Valid Time vs. Transaction Time, append-only raw storage, and why `known_to` is derived via `LEAD()` rather than stored.
   - **`018-duckdb-polars-asof-engine.md`:** Compares Feast vs. custom DuckDB/Polars ASOF engine for historical backtesting.
   - **`019-hash-verified-manifest.md`:** Explains content-addressable SHA-256 data snapshots, environment lockfiles, and Git SHA tracking for deterministic backtest replay.
   - **`020-static-ast-vs-runtime-canary-leakage-detection.md`:** Explains static AST analysis vs. dynamic runtime canaries (pre-execution syntax linting vs. runtime invariant enforcement, what each catches, and why defense-in-depth is necessary).
2. **Repository Hygiene:**
   - Remove temporary scratch files, check `.gitignore` ignores `/data/*.parquet` and cache directories.
   - Run a clean clone test in a fresh virtual environment:
     ```bash
     git clone <repo>
     pip install -e .
     pytest -v tests/canaries/
     ledger run-comparison
     ledger lint examples/sample_strategy.py
     ledger verify-manifest manifests/latest.json
     ```
3. **Rehearse Technical Interview Questions:**
   - Practice the 4 core interview defenses (Bitemporality, Dynamic CAF, ASOF joins, AST Linter vs Canary assertions).

---

## 3. Key Presentation Traps to Avoid in Week 5
- **Trap 1:** Burying the thesis under complex setup instructions. Make sure any reviewer can install with `pip install -e .` and run `pytest tests/canaries/` and `ledger lint` in under 60 seconds.
- **Trap 2:** Making unsubstantiated benchmark claims or test coverage numbers. Base all README claims strictly on actual test suites and deterministic backtest runs.
- **Trap 3:** Building generic toy dashboards that distract from core systems engineering. Focus on developer safety tooling, high-throughput engines, and strict data integrity proofs.
