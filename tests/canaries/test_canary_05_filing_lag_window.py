"""Canary 05: fiscal period end does not imply filing availability."""

from datetime import datetime, timezone

import polars as pl

from ledger.core.bitemporal import derive_known_to_polars
from ledger.core.calendars import get_actionable_timestamp
from ledger.features.engine import join_features_as_of
from tests.canaries.conftest import (
    assert_leaky_diverges,
    assert_no_lookahead,
    build_fundamental,
    leaky_same_day_filing,
)

UTC = timezone.utc
SEC_ID = "SEC_AAPL_001"


def test_filing_lag_hides_q1_until_sec_acceptance() -> None:
    """Q1 data is unavailable after quarter-end but before the 10-Q is filed."""
    q1_acceptance = datetime(2023, 5, 10, 16, 30, tzinfo=UTC)
    q1_known_from = get_actionable_timestamp(q1_acceptance)
    assert q1_known_from == q1_acceptance

    filings = pl.concat(
        [
            build_fundamental(
                period_end="2022-12-31",
                value=2.10,
                known_from="2023-01-27 16:30 UTC",
                ingestion_seq=1,
            ),
            build_fundamental(
                period_end="2023-03-31",
                value=1.52,
                known_from=q1_known_from,
                ingestion_seq=2,
            ),
        ]
    )
    filings_view = derive_known_to_polars(
        filings,
        partition_by=["sec_id", "metric_name"],
    )
    observations = pl.DataFrame(
        {
            "sec_id": [SEC_ID] * 4,
            "observation_timestamp": [
                datetime(2023, 4, 15, 12, 0, tzinfo=UTC),
                datetime(2023, 5, 10, 16, 29, 59, tzinfo=UTC),
                q1_known_from,
                datetime(2023, 5, 15, 12, 0, tzinfo=UTC),
            ],
        }
    )

    pit = join_features_as_of(observations, [filings_view])
    assert_no_lookahead(
        pit.select("metric_value"),
        pl.DataFrame({"metric_value": [2.10, 2.10, 1.52, 1.52]}),
    )

    leaky = leaky_same_day_filing(filings, observations)
    assert_leaky_diverges(
        leaky.select("metric_value"),
        pl.DataFrame({"metric_value": [2.10, 2.10, 1.52, 1.52]}),
    )
    assert leaky["metric_value"].to_list() == [1.52, 1.52, 1.52, 1.52]
