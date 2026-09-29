---
title: Claims Ledger
description: Every quantitative and capability claim in this repository, with its verification method
---

# Claims Ledger

Documentation drifts silently: a number gets copied, a test gets renamed, a
capability gets described in terms of a library that was later swapped. This
file exists so that a claim cannot be repeated without also repeating how to
check it.

**Rule for this repository: a claim may only appear in user-facing docs if it
has a row here.** If you cannot say how a number was produced, it does not go
in the README.

Last verified: 2026-09-29, on commit `8d8477c` of `fix/mypy-tests`, Python
3.10.11, Windows, polars 1.x, local `.venv`.

---

## 1. How to reproduce everything in this file

```bash
# Canary count and exact test names
pytest tests/canaries --collect-only -q

# Canary results
ledger canaries

# Test suite, type check, lint
pytest
mypy . --no-incremental
ruff check .
ruff format --check .

# Linter accuracy
pytest tests/unit/test_linter_corpus.py -v

# ASOF join latency at four scales
python benchmarks/scale_probe.py

# Fresh demo comparison, writes metrics.json
ledger run-comparison
```

---

## 2. Test and suite counts

| Claim | Value | How verified | Notes |
| :--- | :--- | :--- | :--- |
| Canary tests | 16 | `pytest tests/canaries --collect-only -q` | Across 8 files. 6 leakage modes, 1 demo-sanity file, 1 harness-self-test file. |
| Canary leakage modes | 6 | Files `test_canary_01`–`test_canary_06` | Canary 07 is demo invariants, not a leakage mode. |
| Full test suite | 226 passed, 1 deselected | `pytest` | The deselected test is marked `integration` and needs network. |
| Property tests | 6 | `pytest tests/property` | Hypothesis-based bitemporal invariants. |
| Source files type-checked | 88 | `mypy . --no-incremental` | Includes `tests/`, `benchmarks/`, `examples/`. |
| Leakage linter rules | 4 (`L001`–`L004`) | `ledger/tools/linter.py` | Negative shift/index, unwindowed statistics, unbounded `ffill`, non-PIT join keys. |
| Linter corpus size | 12 leaky, 18 compliant | `tests/corpus/linter_corpus.py` | |
| ADRs | 8 files, numbered two ways | `docs/adr/` | `0001`-style and `001`-style both present. Known inconsistency. |

**Historical claim that was wrong:** `docs/canary_catalog.md` previously said
"all 10 canaries" and printed 10 test names, 3 of which (`test_leaky_pipeline_diverges_from_truth`,
`test_assertion_helpers_detect_divergence`, `test_leaky_same_day_filing_diverges`)
do not exist in the codebase. The actual canary-05 test is
`test_filing_lag_hides_q1_until_sec_acceptance`, not the printed
`test_filing_lag_prevents_prior_quarter_leakage`. The catalog now lists only
names that `pytest --collect-only` reports.

`WAR_LOG.md` still contains "all 10 tests" in two places. Those are dated
historical entries describing a run at the time, and rewriting them would falsify
the record. The log is not user-facing documentation of current state.

---

## 3. Quantitative claims about the demo backtest

Source: `ledger run-comparison`, `artifacts/runs/<run_id>/metrics.json`.
Reproducible because `generate_synthetic_data` is seeded.

| Metric | Leaky arm | PIT-corrected arm | Overstatement |
| :--- | ---: | ---: | ---: |
| Cumulative return | +46.64% | +30.70% | +15.95 pp |
| Sharpe ratio | 0.558 | 0.423 | +0.136 |
| CAGR | 10.07% | 6.94% | +3.13 pp |
| Max drawdown | -28.29% | -28.29% | 0.00 pp |
| Annualized volatility | 20.25% | 20.09% | +0.17 pp |
| Calmar ratio | 0.356 | 0.245 | +0.111 |
| Win rate | 46.07% | 45.97% | +0.10 pp |
| Mean turnover | 0.264 | 0.270 | -0.006 |

**What this table does and does not show.** The leak inflates return and Sharpe
by a large margin while leaving drawdown essentially unchanged. It does *not*
show a drawdown catastrophe. Earlier README wording implied the leak made
drawdown worse; it does not, and `metrics.json` records the delta as
`-5.55e-16`, which is floating-point zero.

The mechanism: the generator emits a single 4:1 forward split at the date
midpoint. Pre-split closes are 4x the post-split economic level, so the
as-traded series steps **down** by 4x at the midpoint. The leaky arm reads the
pre-adjusted close, which is smooth across it, and passes `splits=None`, so it
never accounts for the level change at all. The PIT arm reconstructs as-traded
levels and applies `CAF = 1 / split_ratio = 0.25` across the boundary.

**A previous version of this sentence was backwards.** It described "the
unadjusted 4x level jump inflating cumulative return", implying an upward jump
and implying the leaky arm was reading unadjusted prices. The leaky arm reads
the *pre-adjusted* series, and the as-traded step is downward. The magnitude
was right; the direction and the mechanism were not.

---

## 4. Corporate action factor convention

| Claim | Value | How verified |
| :--- | :--- | :--- |
| CAF for a 4:1 split | `1 / split_ratio` = `0.25` | `ledger/ingestion/corporate_actions.py`, `tests/unit/test_raw_price_basis.py` |
| CAF direction | Divides stored price down to post-split basis | Same. Never multiplies. |
| `known_from` for daily feeds | `ex_date` session close + 15 min, 16:15 ET | `docs/adr/005-corporate-action-known-from-convention.md` |

`docs/adr/005` previously stated `CAF = 4.0` after the split was known. That
inverts the adjustment: it would multiply a $400 July 2020 print to $1,600
instead of dividing to $100. Corrected to `1 / split_ratio = 0.25` with the
reasoning stated inline, since the inverted form is the intuitive-but-wrong one.

---

## 5. As-traded price storage

Claim: prices on disk are as-traded, not vendor pre-adjusted.

Verified against ground truth in `tests/unit/test_raw_price_basis.py`:

| Bar | Stored | Derivation |
| :--- | ---: | :--- |
| AAPL 2020-01-02 | $300.35 | vendor $75.0875 × 4 (2020-08-31 split) |
| AAPL 2020-08-28 | $499.23 | pre-split, unaffected by the later 2022-08-25 split |

Vendor `yfinance` returns OHLCV already adjusted across its entire history, so
a bar dated before a later split arrives pre-scaled by it. Storing that under a
table named `raw` is look-ahead. `_undo_full_history_split_adjustment` inverts
it using the full split history, including splits after the requested window.
Volume is left untouched because it is reported as-traded.

A split record retains its `sec_id`, so the ratio can be attributed without
guessing from the ticker.

---

## 6. Leakage linter accuracy

Measured by `tests/unit/test_linter_corpus.py` against 30 labelled cases, with
floors that fail the build on regression.

| Rule | TP | FP | Precision | Recall | Floor |
| :--- | ---: | ---: | ---: | ---: | ---: |
| `L001` negative shift/index | 5 | 3 | 62.5% | 100% | 60% |
| `L002` unwindowed statistics | 4 | 1 | 80.0% | 100% | 75% |
| `L003` unbounded `ffill` | 1 | 0 | 100% | 100% | 90% |
| `L004` non-PIT join key | 3 | 0 | 100% | 100% | 90% |

**Why the floors are not 1.0.** The three `L001` false positives are bare
`Name` subscripts: `equity[-1]`, `history[-20:]`, `ticker[-1]`. Given
`equity[-1]`, no amount of syntax inspection distinguishes a final-value read
on a materialised list from a peek at the last bar of a Series. Exempting them
requires a name allowlist, which is a guess that rots silently the first time a
series is named something else. A type-aware linter is the real fix; it is not
built.

**One case is deliberately not exempt.** A bare `shift(-1)` with no window is
used correctly in `ledger/backtest/simulation.py`, but is syntactically
identical to a real peek at a future price. Labelling it compliant would teach
the rule to ignore an actual leak, so it remains a documented false positive.

**False positives on Ledger's own code:** 8, down from 10 before the rule
learned to recognise windowed `shift(-1).over(...)`. All 8 are the
irreducible `Name` subscript case.

---

## 7. ASOF join performance

Measured by `benchmarks/scale_probe.py`, median of 5 runs:

| Observations | Median |
| ---: | ---: |
| 1,500 | 3.1 ms |
| 6,000 | 3.0 ms |
| 12,000 | 3.8 ms |
| 30,000 | 6.1 ms |

Roughly linear with a large constant floor, which is the shape a vectorized
join should have. The test budget is 250 ms at 30,000 observations, about 40x
the measured median, chosen so a busy CI runner does not cause a flake while an
accidental per-row loop still fails by orders of magnitude.

The previous test was named `test_benchmark_1000_observations_sub_100ms` and
ran 1,500 observation queries — small enough that a per-row Python loop would
still have passed the 100 ms budget.

---

## 8. Capability claims

| Claim | Status | How verified | Caveat |
| :--- | :--- | :--- | :--- |
| TLA+ formal specification | Present | `docs/formal/Ledger.tla`, `Ledger.cfg`, `tlc_run_log.txt` | A prior TLC run log is committed. TLC is **not** invoked by CI, so a change to the spec or the model could go unverified. |
| Docker execution | Present | `Dockerfile`, `docker-compose.yml` | Requires Docker; not run in CI. |
| Hash manifest verification | Works | `ledger verify-manifest`, 8 tests in `tests/unit/test_manifest.py` | Digests are **unsigned**. Establishes reproducibility, not authorship. |
| PDF tear-sheet generation | Works | `ledger run-comparison` writes `report.pdf` into the run directory | No separate command; it is a by-product of `run-comparison`. Output is untracked, so regenerate locally. |
| SEC EDGAR ingestion | **Not built** | — | ADR-005 lists it as a Weeks 6–7 extension. The `known_from` convention is the daily-feed approximation, not filing acceptance time. |
| Streaming / intraday | **Not supported** | — | Daily bars only. Stated in README scope. |
| 16 canaries, all passing | True | `ledger canaries` | ~4s. |
| "Zero look-ahead bias" | **Not claimed** | — | The system reduces specific, enumerated leakage modes. Unenumerated modes may exist. |

---

## 9. Known false or overstated claims, current status

| Location | Claim | Status |
| :--- | :--- | :--- |
| `docs/canary_catalog.md` | "10 canaries", 6 non-existent test names | **Fixed** — now lists the 16 real tests. |
| `docs/adr/005` | `CAF = 4.0` | **Fixed** — `1 / split_ratio = 0.25`. |
| `README.md` | "unadjusted 4x level jump" inflating returns | **Fixed** — the step is downward, and the leaky arm reads pre-adjusted prices. |
| `ledger/backtest/runner.py` | Seeded catalog is pre-adjusted | **Fixed** — it is as-traded; the flag is for vendor feeds. |
| `Fix_Plan.md` | Polars warning "✅ Verified Fixed" | **Fixed** — downgraded to partial, with the pytest-vs-CLI distinction documented. |
| `DEMO_RUNBOOK.md` | Link to gitignored `.mp4` | **Fixed** — link removed, explanation retained. |
| `README.md`, `docs/ml_feature_store_architecture.md` | "Cryptographic manifest", "full regulatory compliance" | **Fixed** — renamed to hash manifest; the unsigned limitation is stated. |
| `docs/canary_catalog.md` | "under 2 seconds" | **Corrected** — ~4s. |
| `Fix_Plan.md` | `uv.lock` missing while referenced in ADRs | Open. |
| Repository | No `LICENSE` | Open. |
| `docs/adr/` | Dual numbering schemes | Open. |
| `ledger lint <directory>` | Raises `PermissionError` | Open. Single-file only. |
| `engine.py:310` | Polars sortedness warning | Open outside pytest. See `Fix_Plan.md`. |

---

## 10. House rules this file exists to enforce

1. **No number without a method.** Every figure above names the command that
   produces it.
2. **No test name without `--collect-only`.** Do not type a test name from
   memory.
3. **Do not state a capability that CI does not exercise without saying so.**
   TLA+ and Docker are real but unchecked; SEC EDGAR is not built.
4. **When correcting a wrong claim, record what it said and why it was wrong**
   in section 9. A silent fix loses the reason, and the same error returns.
5. **Do not rewrite dated historical logs.** `WAR_LOG.md` entries describe runs
   as they happened. Current-state claims live here and in the docs.
