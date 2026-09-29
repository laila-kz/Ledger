"""Deterministic synthetic market data generator for the offline demo and tests.

The generator produces *realistic* price paths using Geometric Brownian Motion
with a fixed seed, plus a quarterly fundamentals stream containing a genuine
**restatement** (a 10-Q/A amendment that revises a previously filed EPS).

Why a restatement rather than a split
-------------------------------------
A split is return-neutral once the adjustment factor is applied correctly, so a
split cannot by itself explain a difference in realised returns. The project's
real thesis is knowledge time: a naive pipeline treats a filing as available on
its period-end date, so it consumes an amendment before anyone could have known
about it. That is what this generator models.

It also still emits a 4:1 split for ``sec_ids[0]`` because a split distorts
*level-dependent* features -- including the strategy's ``price > sma_50d``
trend filter -- which is the second, subtler class of leakage the demo shows.

Design
------
* **GBM parameters**: annualised drift mu = 0.07, annualised volatility
  sigma = 0.28, daily shocks scaled by sigma / sqrt(252).
* **Split**: a 4-for-1 forward split at the date midpoint for ``sec_ids[0]``.
  Pre-split raw closes are 4x the post-split economic level.
* **Restatement**: ``restated_sec_id`` files EPS for the fiscal quarter ending
  the split ex-date, then amends it one quarter later. The amendment carries a
  later ``known_from`` and a 30% downward revision.
* **Seed**: ``PRNG_SEED`` is module level; tests may pass an explicit seed.
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta, timezone

import polars as pl

from ledger.core.bitemporal import derive_known_to_polars

UTC = timezone.utc

# ---------------------------------------------------------------------------
# GBM parameters (annualised)
# ---------------------------------------------------------------------------
_MU = 0.07  # annual drift
_SIGMA = 0.28  # annual volatility
_TRADING_DAYS = 252

# Time-of-day conventions.
#
# A bar is stamped `known_from` at 21:15 UTC, ten minutes after the 21:05 close
# it reports. The observation must land *after* that stamp, otherwise the
# point-in-time arm never sees the current day's bar and silently trades on a
# one-day-lagged price series. That lag is not the leak this demo is about: it
# would apply to every security on every day and swamp the restatement signal.
# Keeping the observation after confirmation means both arms see the same
# prices, and the only differences are the availability rules under test.
#
# Fundamentals are filed at 17:30 UTC on the filing date, after the 16:15 close,
# so a filing is invisible to a 21:30 observation on the day it is filed.
_CONFIRMATION_TIME = time(21, 15)
OBSERVATION_TIME = _OBSERVATION_TIME = time(21, 30)
_FILING_TIME = time(17, 30)

# Magnitude of the EPS restatement. Large enough to flip a rank, small enough
# that the leaky arm is not handed an absurd number.
_EPS_BASE = 5.0
_EPS_REVISION = -0.30  # 30% downward amendment

# Public aliases: the strategy's EPS screen must sit strictly between the
# original and the amended value for the restatement to change any decision.
EPS_BASE = _EPS_BASE
EPS_REVISION = _EPS_REVISION
EPS_AMENDED = _EPS_BASE * (1.0 + _EPS_REVISION)
EPS_SCREEN_THRESHOLD = (EPS_BASE + EPS_AMENDED) / 2.0

# Module-level default seed. All four canary-07 invariants pass with this value.
# Change only if regenerating the demo dataset; re-run tests/canaries/ after.
PRNG_SEED: int = 20240106

FILING_SCHEMA: dict[str, pl.DataType] = {
    "sec_id": pl.String(),
    "filing_type": pl.String(),
    "fiscal_period_end": pl.Date(),
    "metric_name": pl.String(),
    "metric_value": pl.Float64(),
    "known_from": pl.Datetime("us", "UTC"),
    "is_restatement": pl.Boolean(),
    "ingestion_seq": pl.Int64(),
}

SPLIT_SCHEMA: dict[str, pl.DataType] = {
    "sec_id": pl.String(),
    "ex_date": pl.Date(),
    "split_ratio": pl.Float64(),
    "known_from": pl.Datetime("us", "UTC"),
}


def _previous_quarter_end(d: date) -> date:
    """Fiscal quarter-end immediately before ``d`` (03-31, 06-30, 09-30, 12-31)."""
    month = ((d.month - 1) // 3) * 3
    year = d.year
    if month == 0:
        month = 12
        year -= 1
    day = 31 if month in (3, 12) else 30
    return date(year, month, day)


def _fiscal_quarter_ends(start: date, end: date) -> list[date]:
    """Every quarter-end in ``[start, end]``."""
    ends: list[date] = []
    year, month = start.year, ((start.month - 1) // 3 + 1) * 3
    while True:
        day = 31 if month in (3, 12) else 30
        candidate = date(year, month, day)
        if candidate > end:
            break
        if candidate >= start:
            ends.append(candidate)
        month += 3
        if month > 12:
            month -= 12
            year += 1
    return ends


def generate_fundamentals(
    sec_ids: tuple[str, ...],
    fiscal_period_ends: list[date],
    restated_sec_id: str,
    window_start: date,
    window_end: date,
) -> pl.DataFrame:
    """Build a bitemporal fundamentals stream with one genuine EPS restatement.

    Two deliberate design choices keep the demo honest:

    *Carry-in filing.* A prior-quarter filing is prepended and stamped known at
    the window start. Without it the point-in-time arm would see a null EPS for
    the first ~60 days while the control arm saw a value, producing a divergence
    that has nothing to do with the restatement being demonstrated.

    *Mid-window restatement.* The amended period is the candidate whose
    amendment date lands nearest the window midpoint, so the effect is visible
    in the equity curve instead of hiding in the final quarter. The divergence
    window itself is fixed at ~75 days (period end to amendment) wherever it
    sits, because both arms converge once the amendment is known.

    The returned frame carries ``known_from`` (when we learned it) and, via
    :func:`derive_known_to_polars`, a derived ``known_to`` so that the original
    filing is masked at any observation at or after the amendment.
    """
    filing_lag = timedelta(days=30)
    amendment_lag = timedelta(days=45)
    carry_in = _previous_quarter_end(fiscal_period_ends[0])
    periods = [carry_in, *fiscal_period_ends]

    def _amendment_date(period_end: date) -> date:
        return period_end + filing_lag + amendment_lag

    candidates = [p for p in fiscal_period_ends if _amendment_date(p) <= window_end]
    if candidates:
        midpoint = window_start + (window_end - window_start) / 2
        amendment_period = min(candidates, key=lambda p: abs((_amendment_date(p) - midpoint).days))
    else:
        amendment_period = fiscal_period_ends[-1]

    rows: list[dict[str, object]] = []
    seq = 0

    for sec_id in sec_ids:
        for period_end in periods:
            seq += 1
            is_carry_in = period_end == carry_in
            if is_carry_in:
                # Available from the first observation of the window.
                filed_on = window_start
            else:
                # Stagger by security so known_from values are distinct per name.
                filed_on = period_end + filing_lag + timedelta(days=seq % 3)
            rows.append(
                {
                    "sec_id": sec_id,
                    "filing_type": "10-Q",
                    "fiscal_period_end": period_end,
                    "metric_name": "eps",
                    "metric_value": _EPS_BASE,
                    "known_from": datetime.combine(filed_on, _FILING_TIME, tzinfo=UTC),
                    "is_restatement": False,
                    "ingestion_seq": seq,
                }
            )

            if sec_id == restated_sec_id and period_end == amendment_period:
                seq += 1
                amended_on = filed_on + amendment_lag
                rows.append(
                    {
                        "sec_id": restated_sec_id,
                        "filing_type": "10-Q/A",
                        "fiscal_period_end": period_end,
                        "metric_name": "eps",
                        "metric_value": _EPS_BASE * (1.0 + _EPS_REVISION),
                        "known_from": datetime.combine(amended_on, _FILING_TIME, tzinfo=UTC),
                        "is_restatement": True,
                        "ingestion_seq": seq,
                    }
                )

    filings = pl.DataFrame(rows, schema=FILING_SCHEMA)
    return derive_known_to_polars(
        filings,
        partition_by=["sec_id", "metric_name", "fiscal_period_end"],
    )


def leaky_fundamentals_view(fundamentals: pl.DataFrame) -> pl.DataFrame:
    """Deliberately misdate filings by their fiscal period end.

    This is the classic point-in-time bug the demo is built to expose: a
    pipeline that treats ``fiscal_period_end`` as the availability date rather
    than the filing date. The 10-Q/A revision then becomes visible from the
    period end instead of 45 days later, so the control arm screens on a number
    that did not exist when the decision was made.

    The derived ``known_to`` is dropped as well, because a naive pipeline has no
    concept of a superseded version -- it simply keeps the latest row. The
    amendment is nudged one second past the original so the backward ASOF join
    resolves the tie deterministically in favour of the revised value.
    """
    return (
        fundamentals.with_columns(
            pl.col("fiscal_period_end")
            .cast(pl.Datetime("us"))
            .dt.replace_time_zone("UTC")
            .alias("known_from"),
            pl.when(pl.col("is_restatement"))
            .then(pl.duration(seconds=1))
            .otherwise(pl.duration(seconds=0))
            .alias("_tiebreak"),
        )
        .with_columns((pl.col("known_from") + pl.col("_tiebreak")).alias("known_from"))
        .drop("_tiebreak", "known_to")
    )


def generate_synthetic_data(
    sec_ids: tuple[str, ...],
    start_date: date,
    end_date: date,
    seed: int = PRNG_SEED,
    restated_sec_id: str | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Generate deterministic synthetic OHLCV, corporate actions, and fundamentals.

    Returns
    -------
    raw_prices : pl.DataFrame
        Unadjusted price bars ``[sec_id, trade_date, open, high, low, close,
        volume, known_from]``. For the split security, closes before the
        midpoint are 4x the post-split economic level.
    preadjusted : pl.DataFrame
        Vendor pre-adjusted closes ``[sec_id, trade_date, close]`` -- the leaky
        control's price input. Already reflects every future split.
    splits : pl.DataFrame
        A single 4:1 forward split for ``sec_ids[0]`` at the date midpoint.
    fundamentals : pl.DataFrame
        Bitemporal EPS filings including one restatement, with derived
        ``known_to``.
    """
    dates = _business_days(start_date, end_date)
    if not dates:
        raise ValueError("No weekday dates found within the specified date range.")

    rng = random.Random(seed)
    dt = 1.0 / _TRADING_DAYS
    daily_drift = _MU * dt - 0.5 * _SIGMA**2 * dt  # Ito correction
    daily_vol = _SIGMA * math.sqrt(dt)

    midpoint_date = dates[len(dates) // 2]
    split_sec_id = sec_ids[0]
    if restated_sec_id is None:
        restated_sec_id = sec_ids[min(1, len(sec_ids) - 1)]

    # -----------------------------------------------------------------------
    # Independent GBM paths (one per sec_id)
    # -----------------------------------------------------------------------
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
    # Build raw_prices (unadjusted) and preadjusted (vendor-style) series
    # -----------------------------------------------------------------------
    raw_rows: list[dict[str, object]] = []
    preadj_rows: list[dict[str, object]] = []

    for idx, d in enumerate(dates):
        known_from = datetime.combine(d, _CONFIRMATION_TIME, tzinfo=UTC)
        for sec_id in sec_ids:
            economic_price = paths[sec_id][idx]

            # Raw price: before the split, the unadjusted print is 4x higher.
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

            # Leaky control: the vendor-adjusted economic level, treated as
            # available at the start of its calendar day. That day-start
            # availability is precisely the assumption that leaks.
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
            "known_from": datetime.combine(midpoint_date, _CONFIRMATION_TIME, tzinfo=UTC),
        }
    ]

    fundamentals = generate_fundamentals(
        sec_ids=sec_ids,
        fiscal_period_ends=_fiscal_quarter_ends(start_date, end_date),
        restated_sec_id=restated_sec_id,
        window_start=start_date,
        window_end=end_date,
    )

    return (
        pl.DataFrame(raw_rows),
        pl.DataFrame(preadj_rows),
        pl.DataFrame(splits_rows, schema=SPLIT_SCHEMA),
        fundamentals,
    )


def _business_days(start: date, end: date) -> list[date]:
    """Return all Mon-Fri dates in ``[start, end]``."""
    result: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


__all__ = [
    "EPS_AMENDED",
    "EPS_BASE",
    "EPS_REVISION",
    "EPS_SCREEN_THRESHOLD",
    "FILING_SCHEMA",
    "OBSERVATION_TIME",
    "PRNG_SEED",
    "SPLIT_SCHEMA",
    "generate_fundamentals",
    "generate_synthetic_data",
    "leaky_fundamentals_view",
]
