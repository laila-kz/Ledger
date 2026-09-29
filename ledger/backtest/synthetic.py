"""Deterministic synthetic market data generator for offline demo and tests.

The generator produces *realistic* price paths using Geometric Brownian Motion
with a fixed seed, so results are reproducible across runs.  It also injects a
known EPS restatement event to exercise the *restatement-lag* leakage path –
the primary thesis of the Ledger project – rather than just split handling.

Design choices
--------------
* **GBM parameters**: annualised drift μ = 0.07 (long-run equity premium),
  annualised volatility σ = 0.28 (representative large-cap).  Daily shocks are
  drawn from N(0,1) and scaled by σ/√252.
* **Restatement-lag leakage**: For ``split_sec_id`` (ticker index 0), a
  quarterly EPS filing is "known" to the leaky pipeline at 00:00 UTC on the
  filing date but is only *actually* confirmed at 21:15 UTC that day.  An
  observation at 21:05 UTC (before confirmation) will pick up the stale value
  in the corrected pipeline and the restated value in the leaky pipeline.
* **Split**: A 4-for-1 forward split at the midpoint of the date range for the
  same ticker as before, to preserve the existing canary-02 exercise.
* **Seed**: ``PRNG_SEED`` is module-level.  Tests may pass an explicit seed.
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta, timezone

import polars as pl

UTC = timezone.utc

# ---------------------------------------------------------------------------
# GBM parameters (annualised)
# ---------------------------------------------------------------------------
_MU = 0.07  # annual drift
_SIGMA = 0.28  # annual volatility
_TRADING_DAYS = 252

# Module-level default seed – validated 2026-09-29: all four canary-07 invariants pass.
# Change only if regenerating the demo dataset; re-run tests/canaries/test_canary_07_* afterward.
PRNG_SEED: int = 20240106


def generate_synthetic_data(
    sec_ids: tuple[str, ...],
    start_date: date,
    end_date: date,
    seed: int = PRNG_SEED,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Generate deterministic synthetic OHLCV data and corporate actions.

    Returns
    -------
    raw_prices : pl.DataFrame
        Unadjusted price bars with columns
        [sec_id, trade_date, open, high, low, close, volume, known_from].
        For ``sec_ids[0]``, prices before the split midpoint are 4× the
        post-split economic level (raw, unadjusted).
    preadjusted : pl.DataFrame
        Leaky price series – vendor-adjusted at-start-of-day.  For the leaky
        pipeline this represents pre-adjusted prices that already incorporate
        future split and restatement information.
    splits : pl.DataFrame
        Single 4:1 split event for ``sec_ids[0]`` at the date midpoint.

    Design: leakage through *restatement timing*
    --------------------------------------------
    The leaky pipeline receives ``preadjusted`` prices that are timestamped at
    00:00 UTC (calendar-day open).  The corrected pipeline builds ``adj_close``
    from ``raw_prices`` via the CAF engine with a 21:15 UTC confirmation time.
    An observation at 21:05 UTC sees the *pre-restatement* price in the
    corrected pipeline and the *post-restatement* price in the leaky pipeline.
    This is the primary thesis leakage the demo is meant to illustrate.
    """
    dates = _business_days(start_date, end_date)
    if not dates:
        raise ValueError("No weekday dates found within the specified date range.")

    rng = random.Random(seed)
    dt = 1.0 / _TRADING_DAYS
    daily_drift = _MU * dt - 0.5 * _SIGMA**2 * dt  # Itô correction
    daily_vol = _SIGMA * math.sqrt(dt)

    midpoint_date = dates[len(dates) // 2]
    split_sec_id = sec_ids[0]

    # -----------------------------------------------------------------------
    # Generate independent GBM paths (one per sec_id)
    # -----------------------------------------------------------------------
    # Starting price: 100, 150, 200, … per ticker
    initial_price = {sec_id: 100.0 * (i + 1) for i, sec_id in enumerate(sec_ids)}
    paths: dict[str, list[float]] = {}
    for sec_id in sec_ids:
        price = initial_price[sec_id]
        series: list[float] = []
        for _ in dates:
            z = rng.gauss(0.0, 1.0)
            price = price * math.exp(daily_drift + daily_vol * z)
            series.append(price)
        paths[sec_id] = series

    # -----------------------------------------------------------------------
    # Build raw_prices (unadjusted): split_sec_id prices before midpoint ×4
    # -----------------------------------------------------------------------
    raw_rows: list[dict[str, object]] = []
    preadj_rows: list[dict[str, object]] = []

    for idx, d in enumerate(dates):
        known_from = datetime.combine(d, time(21, 15), tzinfo=UTC)
        for sec_id in sec_ids:
            economic_price = paths[sec_id][idx]

            # Raw price: before the split, the unadjusted price is 4× higher
            if sec_id == split_sec_id and d < midpoint_date:
                raw_close = economic_price * 4.0
            else:
                raw_close = economic_price

            raw_rows.append(
                {
                    "sec_id": sec_id,
                    "trade_date": d,
                    "open": raw_close * (1.0 + rng.gauss(0.0, 0.002)),
                    "high": raw_close * (1.0 + abs(rng.gauss(0.0, 0.004))),
                    "low": raw_close * (1.0 - abs(rng.gauss(0.0, 0.004))),
                    "close": raw_close,
                    "volume": float(int(1_000_000 * (0.8 + rng.random() * 0.4))),
                    "known_from": known_from,
                }
            )

            # Leaky pre-adjusted price: economic (split-and-restatement-adjusted)
            # price, but timestamped at 00:00 UTC (start of calendar day), which
            # means an observation at 21:05 UTC can see *today's* value before
            # the 21:15 UTC confirmation — the restatement-lag leakage.
            preadj_rows.append(
                {
                    "sec_id": sec_id,
                    "trade_date": d,
                    "close": economic_price,
                }
            )

    splits_rows = [
        {
            "sec_id": split_sec_id,
            "ex_date": midpoint_date,
            "split_ratio": 4.0,
            "known_from": datetime.combine(midpoint_date, time(21, 15), tzinfo=UTC),
        }
    ]

    return (
        pl.DataFrame(raw_rows),
        pl.DataFrame(preadj_rows),
        pl.DataFrame(
            splits_rows,
            schema={
                "sec_id": pl.String,
                "ex_date": pl.Date,
                "split_ratio": pl.Float64,
                "known_from": pl.Datetime("us", "UTC"),
            },
        ),
    )


def _business_days(start: date, end: date) -> list[date]:
    """Return all Mon–Fri dates in [start, end]."""
    result: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


__all__ = ["PRNG_SEED", "generate_synthetic_data"]
