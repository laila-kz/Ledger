"""Canary 02: split adjustments must be dynamic and knowledge-time aware."""

from datetime import date, datetime, timezone

import polars as pl

from ledger.features.caf import compute_caf_matrix
from tests.canaries.conftest import (
    assert_leaky_diverges,
    assert_no_lookahead,
    build_price_series,
    build_split,
    leaky_static_adjusted_close,
)

UTC = timezone.utc
SEC_ID = "SEC_AAPL_001"
PRICE_DATE = date(2020, 7, 15)


def test_retroactive_split_adjustment_respects_observation_time() -> None:
    """A future split cannot adjust a historical price before it is known.

    Ledger's locked CAF convention multiplies historical prices by the split
    ratio. Therefore the post-split July value is $1,600, not the guide's older
    vendor-style $100 wording: $400 * 4 = $1,600 and CAF = 4.0.
    """
    prices = build_price_series(
        start=PRICE_DATE,
        end="2020-08-31",
        base_price=400.0,
        daily_step=0.0,
    )
    splits = build_split(
        ex_date="2020-08-31",
        ratio=4.0,
        known_from="2020-08-31 20:15 UTC",
        announcement_date="2020-08-11",
    )
    observations = pl.DataFrame(
        {
            "sec_id": [SEC_ID, SEC_ID],
            "observation_timestamp": [
                datetime(2020, 7, 15, 21, 0, tzinfo=UTC),
                datetime(2020, 9, 1, 21, 0, tzinfo=UTC),
            ],
        }
    )

    pit = compute_caf_matrix(prices, splits, observations).filter(
        pl.col("trade_date") == PRICE_DATE
    ).sort("observation_timestamp")
    expected = pl.DataFrame(
        {
            "caf": [1.0, 4.0],
            "adj_close": [400.0, 1600.0],
        }
    )
    assert_no_lookahead(pit.select(["caf", "adj_close"]), expected)

    leaky = leaky_static_adjusted_close(prices, splits).filter(
        pl.col("trade_date") == PRICE_DATE
    )
    assert_leaky_diverges(
        leaky.select("adj_close"),
        pl.DataFrame({"adj_close": [400.0]}),
    )
    assert leaky["adj_close"].item() == 1600.0
