# Changelog

Notable changes to Ledger, newest first. Each entry records what changed and
why it mattered for the integrity guarantees described in the README.

## Unreleased

### Fixed

- **As-traded prices for real-data runs.** Ingestion stored vendor
  split-adjusted closes under a table named `raw`, so a 2020 bar already
  carried the effect of a 2022 split. `_undo_full_history_split_adjustment` now
  inverts the vendor adjustment using the full split history, including splits
  after the requested window, and leaves volume untouched because it is
  reported as-traded. Ground truth: AAPL 2020-01-02 is stored at $300.35
  (75.0875 × 4) and 2020-08-28 at $499.23.
- **Replay is idempotent.** Re-ingesting duplicate rows no longer produces
  duplicate intervals. Covered by `test_idempotent_reingestion`.
- **`known_from` stamped at true publication time.** Corporate action facts
  were stamped at a fixed boundary rather than the actual New York midday
  publication instant.
- **Corporate action factor is now `1 / split_ratio`.** The CAF matrix was
  inverted.
- **Split records retain `sec_id`.**
- **Windows console encoding.** CLI commands on Windows PowerShell raised
  `UnicodeEncodeError` when writing UTF-8 checkmarks to a legacy `cp1252`
  stdout. `verify_manifest.py` and `seed_week1.py` now reconfigure `sys.stdout`.
- **CI type-checked only the package.** The workflow ran `mypy ledger` while
  `make check` ran `mypy .`, so a green CI badge did not mean the test suite
  type-checked. Both now run `mypy .`; see the 55 test-suite errors fixed below.
- **Linter ignored keyword shift offsets.** `shift(periods=-1)` was not
  detected; only the positional spelling was.
- **Linter flagged windowed `shift(-1).over(...)`.** That is the polars/SQL
  `LEAD()` idiom used to derive interval upper bounds from append-only rows,
  which cannot leak. The rule now distinguishes it from a bare `shift(-1)`.

### Changed

- **Leakage linter accuracy is now measured.** `tests/corpus/linter_corpus.py`
  holds 12 leaky and 18 compliant labelled cases;
  `tests/unit/test_linter_corpus.py` scores per-rule precision and recall
  against floors so the rate cannot silently regress. Measured today: L001
  62.5% precision / 100% recall, L002 80% / 100%, L003 and L004 both 100%.
  The residual false positives are bare `Name` subscripts such as
  `equity[-1]`, which are indistinguishable from a real peek at `prices[-1]`
  without type information. Resolving them needs a type-aware linter.
- **ASOF benchmark scaled to 30,000 observations** (from 1,500) with a 250ms
  budget derived from `benchmarks/scale_probe.py`, which measures the join at
  1.5k / 6k / 12k / 30k observations.
- **"Cryptographic manifest" renamed to "hash manifest."** The manifest
  records unsigned SHA-256 digests. It establishes that a run can be
  re-derived from known inputs; it does not authenticate the run's author.
- **US Eastern timestamps use `ET`.** Generic wall-clock references no longer
  claim the daylight-saving abbreviation. Genuine EDT↔EST contrast in the
  timezone ADR is preserved.
- **README and CHANGELOG separated.** Durable design constraints stay in the
  README; incidental fixes move here.

### Added

- `metrics.json` in every run artifact, with `WAR_LOG.md` and the docs
  refreshed against fresh reproducible runs.
- 55 `mypy` errors fixed across 9 test files: a wrong `_run()` return
  annotation, missing `None` guards on metric results, Polars `.max()` not
  narrowing, and untyped pytest fixtures.
- Explicit DataFrame annotations in `examples/sample_strategy.py`, which only
  surfaced under CI's fresh dependency resolution.

### Known issues

- `ledger lint` accepts a single script and raises `PermissionError` when
  given a directory.
- `docs/canary_catalog.md` still documents 10 canaries; the suite has 16.
- `README.md` describes a 4x level jump where the real 2020-08-31 AAPL 4:1
  split produces a 4.2x move.
- `docs/adr/005-corporate-action-known-from-convention.md` still states
  `CAF = 4.0`.
- `ledger/backtest/runner.py` carries a stale docstring referring to
  pre-adjusted catalogs.
- No `LICENSE` file.
- ADR files are numbered both `0001`-style and `001`-style.
