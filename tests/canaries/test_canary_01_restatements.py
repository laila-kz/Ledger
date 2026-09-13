"""Canary 01: restated fundamentals must respect transaction time."""

from datetime import datetime, timezone

import polars as pl

from ledger.core.bitemporal import derive_known_to_polars
from ledger.features.engine import join_features_as_of
from tests.canaries.conftest import (
    assert_leaky_diverges,
    assert_no_lookahead,
    build_fundamental,
    leaky_latest_fundamental,
)

UTC = timezone.utc
SEC_ID = "SEC_AAPL_001"


def test_restatement_isolated_until_known_from() -> None:
    """The original EPS remains active until the amendment becomes knowable."""
    filings = pl.concat(
        [
            build_fundamental(
                value=1.00,
                known_from="2023-08-01 17:30 UTC",
                ingestion_seq=1,
            ),
            build_fundamental(
                value=0.70,
                known_from="2023-11-15 17:30 UTC",
                ingestion_seq=2,
                is_restatement=True,
            ),
        ]
    )
    filings_view = derive_known_to_polars(
        filings,
        partition_by=["sec_id", "metric_name", "fiscal_period_end"],
    )
    observations = pl.DataFrame(
        {
            "sec_id": [SEC_ID] * 4,
            "observation_timestamp": [
                datetime(2023, 9, 1, 21, 0, tzinfo=UTC),
                datetime(2023, 11, 15, 8, 0, tzinfo=UTC),
                datetime(2023, 11, 15, 18, 0, tzinfo=UTC),
                datetime(2023, 12, 1, 21, 0, tzinfo=UTC),
            ],
        }
    )

    pit = join_features_as_of(observations, [filings_view])
    expected = pl.DataFrame({"metric_value": [1.00, 1.00, 0.70, 0.70]})
    assert_no_lookahead(pit.select("metric_value"), expected)

    leaky = leaky_latest_fundamental(filings, observations)
    assert_leaky_diverges(
        leaky.select("metric_value"),
        expected,
    )
    assert leaky["metric_value"].to_list() == [0.70, 0.70, 0.70, 0.70]
