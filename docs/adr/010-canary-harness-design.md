# ADR 010: Synthetic Leakage Canary Harness

## Status
Accepted - recorded 2026-09-13 (Week 3 Day 1)

## Context

Weeks 1 and 2 established Ledger's append-only storage, bitemporal interval
math, dynamic CAF, and vectorized point-in-time joins. Week 3 needs a shared,
deterministic test world so every leakage canary tests the same contracts instead
of inventing separate fixtures and definitions of leakage.

The harness must make two behaviors explicit:

1. A correct pipeline only uses facts known at the observation timestamp.
2. A deliberately naive pipeline can produce a plausible but incorrect result.

## Decision

`tests/canaries/conftest.py` provides four families of deterministic builders:

- `build_price_series` creates weekday OHLCV bars with UTC `known_from` values.
- `build_split` and `build_dividend` create corporate-action records matching the
  ingestion schema.
- `build_fundamental` creates append-only filing versions, including restatements.
- `build_entity_map` creates append-only ticker aliases with `valid_from`.

It also provides defect-specific reference implementations:

- `leaky_join_on_trade_date` uses valid trade dates instead of knowledge time.
- `leaky_static_adjusted_close` applies all split factors retroactively.
- `leaky_same_day_filing` treats fiscal period end as filing availability.

Assertions are split into `assert_pit_matches_truth` and
`assert_leaky_diverges`. The smaller `assert_no_lookahead` helper is an alias for
checking a pipeline result against its reference truth. This keeps later canary
tests readable while preserving a clear proof that the naive path is wrong.

## Consequences

- Fixtures are real Polars DataFrames and can pass directly to Ledger's CAF and
  ASOF engines.
- All generated timestamps are timezone-aware UTC values, making tests stable
  across developer machines and CI environments.
- The leaky references are intentionally narrow: each models one defect class
  and should not be reused as production behavior.
- Later canaries can append builder outputs and derive `known_to` or `valid_to`
  with the existing bitemporal helpers.
