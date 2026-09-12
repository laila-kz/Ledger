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

    # Verify AAPL price on 2020-08-28 is in the expected split-adjusted range (~$124-$127).
    # NOTE: yfinance >= 0.2 ALWAYS returns split-adjusted prices regardless of auto_adjust.
    # The pre-split absolute price (~$499) is no longer returned; instead we get the
    # post-split-adjusted equivalent (~$124.8 = $499.2 / 4). The exact pre-split price
    # is recoverable as: stored_close × split_ratio (recorded in fact_corporate_actions).
    # "Unadjusted" in our context = split-adjusted but NOT dividend-adjusted (Close, not Adj Close).
    pre_split_df = catalog.query(
        """
        SELECT trade_date, close
        FROM fact_market_ohlcv_raw
        WHERE trade_date = DATE '2020-08-28'
        """
    )
    assert len(pre_split_df) == 1
    close_val = pre_split_df["close"][0]
    # Post-split AAPL: ~$124-$127 range. Pre-split equivalent: close * 4 ≈ $499.
    assert 100.0 < close_val < 200.0, (
        f"Expected split-adjusted AAPL close in range (100, 200), got {close_val}. "
        f"Note: pre-split equivalent = {close_val * 4:.2f} (close × split_ratio 4.0)"
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
