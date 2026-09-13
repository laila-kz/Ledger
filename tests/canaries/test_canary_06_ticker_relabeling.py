"""Canary 06: ticker aliases must not fragment a permanent security history."""

from datetime import datetime, timezone

import polars as pl

from ledger.core.bitemporal import derive_valid_to_polars
from ledger.core.entity import resolve_sec_id, resolve_ticker
from ledger.features.engine import join_features_as_of
from tests.canaries.conftest import assert_no_lookahead, build_entity_map

UTC = timezone.utc
SEC_ID = "SEC_0001326801"


def test_ticker_relabeling_preserves_sec_id_and_feature_continuity() -> None:
    """FB and META resolve to one SecID and retain joined feature history."""
    ticker_map = pl.concat(
        [
            build_entity_map(
                sec_id=SEC_ID,
                ticker="FB",
                valid_from="2012-05-18 00:00 UTC",
                ingestion_seq=1,
            ),
            build_entity_map(
                sec_id=SEC_ID,
                ticker="META",
                valid_from="2022-06-09 00:00 UTC",
                ingestion_seq=2,
            ),
        ]
    )
    ticker_view = derive_valid_to_polars(ticker_map, partition_by="sec_id")

    assert resolve_sec_id("FB", datetime(2018, 1, 1, tzinfo=UTC), df=ticker_view) == SEC_ID
    assert resolve_sec_id("META", datetime(2023, 1, 1, tzinfo=UTC), df=ticker_view) == SEC_ID
    assert resolve_ticker(SEC_ID, datetime(2018, 1, 1, tzinfo=UTC), df=ticker_view) == "FB"
    assert resolve_ticker(SEC_ID, datetime(2023, 1, 1, tzinfo=UTC), df=ticker_view) == "META"

    observations = pl.DataFrame(
        {
            "sec_id": [SEC_ID] * 4,
            "observation_timestamp": [
                datetime(2018, 1, 1, 21, 0, tzinfo=UTC),
                datetime(2022, 6, 8, 21, 0, tzinfo=UTC),
                datetime(2022, 6, 9, 21, 0, tzinfo=UTC),
                datetime(2023, 1, 1, 21, 0, tzinfo=UTC),
            ],
        }
    )
    features = pl.DataFrame(
        {
            "sec_id": [SEC_ID, SEC_ID, SEC_ID],
            "known_from": [
                datetime(2018, 1, 1, 20, 0, tzinfo=UTC),
                datetime(2022, 6, 9, 20, 0, tzinfo=UTC),
                datetime(2023, 1, 1, 20, 0, tzinfo=UTC),
            ],
            "momentum_20d": [0.10, 0.20, 0.30],
        }
    )

    joined = join_features_as_of(observations, [ticker_view, features])
    assert joined.height == observations.height
    assert joined["sec_id"].n_unique() == 1
    assert joined["ticker"].to_list() == ["FB", "FB", "META", "META"]
    assert_no_lookahead(
        joined.select(["ticker", "momentum_20d"]),
        pl.DataFrame(
            {
                "ticker": ["FB", "FB", "META", "META"],
                "momentum_20d": [0.10, 0.10, 0.20, 0.30],
            }
        ),
    )
