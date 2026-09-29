"""Integration smoke test for live yfinance ingestion.

Marked as integration so CI / offline runners can filter it via `pytest -m "not integration"`.
"""

from pathlib import Path

import pytest

from ledger.ingestion.corporate_actions import ingest_corporate_actions
from ledger.ingestion.market_data import ingest_ohlcv
from ledger.storage.catalog import LedgerCatalog


@pytest.mark.integration
def test_live_yfinance_ingestion_smoke(tmp_path: Path) -> None:
    """Smoke test downloading a short window for AAPL and verifying unadjusted split price."""
    test_dir = tmp_path / "data" / "raw"

    # Ingest 3 days around the 2020-08-31 4:1 AAPL split
    entry_ohlcv = ingest_ohlcv(
        tickers=["AAPL"],
        start_date="2020-08-27",
        end_date="2020-09-02",
        base_dir=test_dir,
    )

    if entry_ohlcv is None:
        pytest.skip("Network / yfinance unavailable in test environment.")

    assert entry_ohlcv.row_count >= 3

    # Ingest corporate actions for AAPL in 2020
    entry_ca = ingest_corporate_actions(
        tickers=["AAPL"],
        start_date="2020-01-01",
        end_date="2020-12-31",
        base_dir=test_dir,
    )

    assert entry_ca is not None
    assert entry_ca.row_count >= 1

    catalog = LedgerCatalog(base_dir=test_dir)

    # Verify AAPL on 2020-08-28 is stored AS TRADED (~$499), i.e. the true print
    # from the day before the 2020-08-31 4:1 split.
    #
    # yfinance >= 0.2 always returns prices split-adjusted across the FULL
    # history, so it reports ~$124.81 for this bar -- already divided by a split
    # that had not yet happened. Ingestion undoes that adjustment, so the raw
    # table must hold ~$499.23. A value near $125 means look-ahead leaked back
    # into the store.
    pre_split_df = catalog.query(
        """
        SELECT trade_date, close
        FROM fact_market_ohlcv_raw
        WHERE trade_date = DATE '2020-08-28'
        """
    )
    assert len(pre_split_df) == 1
    close_val = pre_split_df["close"][0]
    assert 450.0 < close_val < 550.0, (
        f"Expected as-traded AAPL close in range (450, 550), got {close_val}. "
        f"A value near 124.8 means the provider's full-history adjustment was "
        f"stored without being undone, reintroducing look-ahead into the raw table."
    )

    # Verify 4:1 split is recorded in fact_corporate_actions
    splits_df = catalog.query(
        """
        SELECT split_ratio
        FROM fact_corporate_actions
        WHERE action_type = 'SPLIT' AND ex_date = DATE '2020-08-31'
        """
    )
    assert len(splits_df) == 1
    assert splits_df["split_ratio"][0] == 4.0

    catalog.close()
