# ADR 014: Comparative Tear-Sheet Methodology

## Status
Accepted - recorded 2026-09-13 (Week 4 Day 3)

## Context
The Day 2 runner produces one close-to-close simulation row per observation
interval. Week 4 needs a reproducible comparison that quantifies the cost of
look-ahead bias without hiding undefined ratios behind `NaN` or infinity.

## Decision
We compute pure performance metrics in `ledger/backtest/metrics.py` and expose
the comparison through `ledger/backtest/tear_sheet.py`.

1. Daily simulation intervals use 252 periods per year by default. Callers can
   pass another frequency explicitly.
2. CAGR uses elapsed time in 365.25-day years.
3. Sharpe uses a zero risk-free rate and sample standard deviation.
4. Drawdown is peak-to-trough, and Calmar is CAGR divided by absolute MDD.
5. Win rate counts strictly positive return periods. Profit factor is gross
   gains divided by absolute gross losses.
6. Delta is always `corrected - leaky`.
7. Undefined metrics return `None` and render as `n/a`.

## Consequences
The structured `TearSheet` supports programmatic consumers while its fixed
width renderer is suitable for terminal output and the future CLI. The report
does not infer frequency from dates; the caller must choose the annualization
constant when using weekly or irregular return series.