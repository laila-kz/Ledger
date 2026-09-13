"""Canary 04: historical universe membership must be point-in-time correct."""

from datetime import datetime, timezone

import polars as pl

from ledger.core.bitemporal import derive_valid_to_polars
from ledger.features.engine import join_features_as_of
from tests.canaries.conftest import assert_leaky_diverges, assert_no_lookahead

UTC = timezone.utc
SEC_ID = "SEC_LEHMAN_001"


def test_survivorship_universe_preserves_historical_membership() -> None:
    """Lehman is present before delisting and absent after it."""
    membership_raw = pl.DataFrame(
        {
            "sec_id": [SEC_ID, SEC_ID],
            "ticker": ["LEH", "LEHMQ"],
            "is_member": [True, False],
            "valid_from": [
                datetime(2000, 1, 1, tzinfo=UTC),
                datetime(2008, 9, 15, tzinfo=UTC),
            ],
            "ingestion_seq": [1, 2],
        }
    )
    membership_view = derive_valid_to_polars(membership_raw, partition_by="sec_id")
    observations = pl.DataFrame(
        {
            "sec_id": [SEC_ID, SEC_ID],
            "observation_timestamp": [
                datetime(2008, 8, 1, 12, 0, tzinfo=UTC),
                datetime(2009, 1, 1, 12, 0, tzinfo=UTC),
            ],
        }
    )

    pit = join_features_as_of(observations, [membership_view])
    assert_no_lookahead(
        pit.select(["ticker", "is_member"]),
        pl.DataFrame(
            {
                "ticker": ["LEH", "LEHMQ"],
                "is_member": [True, False],
            }
        ),
    )

    static_modern_universe = pl.DataFrame(
        {
            "is_member": [False, False],
        }
    )
    assert_leaky_diverges(
        static_modern_universe,
        pl.DataFrame({"is_member": [True, False]}),
    )
    assert static_modern_universe["is_member"].to_list() == [False, False]
