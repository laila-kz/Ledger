"""Canary 03: after-hours filings become actionable at the next session open."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import polars as pl

from ledger.core.calendars import get_actionable_timestamp
from ledger.features.engine import join_features_as_of
from tests.canaries.conftest import assert_leaky_diverges, assert_no_lookahead, build_fundamental

NY_TZ = ZoneInfo("America/New_York")
UTC = timezone.utc
SEC_ID = "SEC_AAPL_001"


def test_after_hours_filing_is_shifted_to_next_session_open() -> None:
    """A Friday 17:00 EDT filing is unavailable until Monday's 09:30 EDT open."""
    raw_event_time = datetime(2023, 4, 14, 17, 0, tzinfo=NY_TZ)
    actionable_time = get_actionable_timestamp(raw_event_time)
    expected_actionable_time = datetime(2023, 4, 17, 9, 30, tzinfo=NY_TZ)
    assert actionable_time == expected_actionable_time
    assert actionable_time.astimezone(UTC) == datetime(2023, 4, 17, 13, 30, tzinfo=UTC)

    filing = build_fundamental(
        value=2.50,
        period_end="2023-03-31",
        known_from=actionable_time,
    )
    observations = pl.DataFrame(
        {
            "sec_id": [SEC_ID] * 3,
            "observation_timestamp": [
                datetime(2023, 4, 14, 19, 59, 0, tzinfo=UTC),
                datetime(2023, 4, 17, 13, 29, 59, tzinfo=UTC),
                datetime(2023, 4, 17, 13, 30, 0, tzinfo=UTC),
            ],
        }
    )

    pit = join_features_as_of(observations, [filing])
    assert_no_lookahead(
        pit.select("metric_value"),
        pl.DataFrame({"metric_value": [None, None, 2.50]}),
    )

    raw_timestamp_filing = filing.with_columns(
        pl.lit(raw_event_time).alias("known_from")
    )
    leaky = join_features_as_of(
        pl.DataFrame(
            {
                "sec_id": [SEC_ID],
                "observation_timestamp": [datetime(2023, 4, 14, 21, 30, tzinfo=UTC)],
            }
        ),
        [raw_timestamp_filing],
    )
    assert_leaky_diverges(
        leaky.select("metric_value"),
        pl.DataFrame({"metric_value": [None]}),
    )
    assert leaky["metric_value"].item() == 2.50


def test_friday_rebalance_cannot_use_after_hours_filing() -> None:
    """The Friday 15:59 EDT rebalance remains blind to the Friday filing."""
    raw_event_time = datetime(2023, 4, 14, 17, 0, tzinfo=NY_TZ)
    filing = build_fundamental(
        value=2.50,
        period_end="2023-03-31",
        known_from=get_actionable_timestamp(raw_event_time),
    )
    rebalance = pl.DataFrame(
        {
            "sec_id": [SEC_ID],
            "observation_timestamp": [datetime(2023, 4, 14, 19, 59, tzinfo=UTC)],
        }
    )

    result = join_features_as_of(rebalance, [filing])
    assert result["metric_value"].item() is None
