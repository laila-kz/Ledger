# ADR 016: Comparative Backtest CLI Conventions

## Status
Accepted - recorded 2026-09-13 (Week 4 Day 5)

## Decision
The comparative backtest is exposed through `argparse` as
`python -m ledger.backtest.run_comparison`.

- `--start-date` and `--end-date` are required and deterministic.
- `--tickers`, `--top-k`, `--cost-bps`, `--initial-capital`, rebalance settings,
  data paths, and artifact root are explicit CLI parameters.
- Tear-sheet output goes to stdout; progress and errors go to stderr.
- Generated artifacts are written beneath `artifacts/runs/<run_id>/`.
- Equity curves, returns, and weights are separate Parquet artifacts; the
  manifest stores their relative filenames and embeds structured metrics.
- `--dry-run` executes data loading, both pipelines, and tear-sheet generation
  but does not write artifacts.
- Exit code 0 means success and 1 means a data or configuration error.

## Consequences
The subprocess integration test exercises the real parser and filesystem
output. Explicit Parquet path options keep tests deterministic and offline,
while the default leaky control path can use the existing yfinance loader when
no pre-adjusted file is supplied.