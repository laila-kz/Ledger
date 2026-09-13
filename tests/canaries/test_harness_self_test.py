"""Self-tests for the shared Week 3 synthetic canary harness."""

from datetime import datetime, timezone

import polars as pl

from ledger.features.caf import compute_caf_matrix
from tests.canaries.conftest import (
    assert_leaky_diverges,
    assert_no_lookahead,
    build_entity_map,
    build_fundamental,
    build_price_series,
    build_split,
    leaky_same_day_filing,
    leaky_static_adjusted_close,
)

UTC = timezone.utc


def test_builders_produce_expected_schemas() -> None:
    prices = build_price_series(start="2020-07-01", end="2020-07-03")
    split = build_split()
    filing = build_fundamental()
    aliases = pl.concat(
        [
            build_entity_map(),
            build_entity_map(ticker="META", valid_from="2022-06-09 UTC", ingestion_seq=2),
        ]
    )

    assert prices.columns == [
        "sec_id",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "known_from",
        "ingestion_seq",
    ]
    assert split["known_from"].dt.time()[0].hour == 20
    assert filing["metric_value"].item() == 1.0
    assert aliases.height == 2


def test_static_adjustment_is_detectably_leaky() -> None:
    prices = build_price_series(
        start="2020-07-28",
        end="2020-08-31",
        base_price=400.0,
        daily_step=0.0,
    )
    splits = build_split()
    observations = pl.DataFrame(
        {
            "sec_id": ["SEC_AAPL_001"],
            "observation_timestamp": [datetime(2020, 8, 15, 15, 0, tzinfo=UTC)],
        }
    )
    pit = compute_caf_matrix(prices, splits, observations).filter(
        pl.col("trade_date") == datetime(2020, 7, 28).date()
    )
    leaky = leaky_static_adjusted_close(prices, splits).filter(
        pl.col("trade_date") == datetime(2020, 7, 28).date()
    )

    assert_no_lookahead(pit.select("adj_close"), pl.DataFrame({"adj_close": [400.0]}))
    assert_leaky_diverges(leaky.select("adj_close"), pl.DataFrame({"adj_close": [400.0]}))


def test_same_day_filing_is_detectably_leaky() -> None:
    filings = build_fundamental(period_end="2023-06-30", value=1.0)
    observations = pl.DataFrame(
        {
            "sec_id": ["SEC_AAPL_001"],
            "observation_timestamp": [datetime(2023, 7, 1, 12, 0, tzinfo=UTC)],
        }
    )
    leaky = leaky_same_day_filing(filings, observations)

    assert leaky["metric_value"].item() == 1.0
    assert_leaky_diverges(leaky.select("metric_value"), pl.DataFrame({"metric_value": [None]}))
