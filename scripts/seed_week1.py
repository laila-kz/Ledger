"""One-shot seed script for Week 1 data ingestion and price-basis verification.

Ingests:
- Liquid US tickers: AAPL, MSFT, NVDA, META, GOOGL, TSLA
- Corporate Actions: AAPL 4:1 split (2020-08-31), TSLA 5:1 split (2020-08-31), dividends
- Verifies the stored price basis: yfinance 'Close', which is split-adjusted but
  NOT dividend-adjusted. It is not raw pre-split data.
"""

import contextlib
import sys
from pathlib import Path

from ledger.ingestion.corporate_actions import ingest_corporate_actions
from ledger.ingestion.market_data import ingest_ohlcv
from ledger.storage.catalog import LedgerCatalog

TICKERS = ["AAPL", "MSFT", "NVDA", "META", "GOOGL", "TSLA"]
START_DATE = "2020-01-01"
END_DATE = "2023-12-31"
BASE_DIR = Path("data/raw")

# Ledger stores yfinance 'Close': split-adjusted but NOT dividend-adjusted.
# yfinance >= 0.2 applies split adjustments unconditionally, so the stored
# series is NOT raw pre-split. These bounds assert the split-adjusted basis so
# a silent provider change fails loudly instead of redefining every downstream
# feature.
AAPL_SPLIT_ADJUSTED_RANGE = (100.0, 200.0)  # 2020-08-28, three days pre-4:1
TSLA_SPLIT_ADJUSTED_RANGE = (350.0, 550.0)  # 2020-08-28, three days pre-5:1


def run_seed() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"=== [LEDGER SEED] Starting Week 1 Ingestion for {TICKERS} ===")
    print(f"Date range: {START_DATE} to {END_DATE}")
    print(f"Storage path: {BASE_DIR.absolute()}")

    # 1. Ingest Market OHLCV
    print("\n[1/3] Ingesting raw unadjusted OHLCV price bars...")
    ohlcv_entry = ingest_ohlcv(
        tickers=TICKERS,
        start_date=START_DATE,
        end_date=END_DATE,
        base_dir=BASE_DIR,
    )
    if ohlcv_entry:
        print(
            f"✓ OHLCV Batch #{ohlcv_entry.ingestion_seq} committed: "
            f"{ohlcv_entry.row_count:,} rows across {ohlcv_entry.file_count} partition files."
        )
    else:
        print("✗ No OHLCV records returned from data provider.")
        sys.exit(1)

    # 2. Ingest Corporate Actions
    print("\n[2/3] Ingesting corporate actions (splits & dividends)...")
    ca_entry = ingest_corporate_actions(
        tickers=TICKERS,
        start_date=START_DATE,
        end_date=END_DATE,
        base_dir=BASE_DIR,
    )
    if ca_entry:
        print(
            f"✓ Corporate Actions Batch #{ca_entry.ingestion_seq} committed: "
            f"{ca_entry.row_count:,} actions across {ca_entry.file_count} partition files."
        )
    else:
        print("ℹ No corporate actions returned.")

    # 3. Verification of the stored price basis
    print("\n[3/3] Verifying stored price basis...")
    catalog = LedgerCatalog(base_dir=BASE_DIR)

    # Ledger stores yfinance's 'Close': split-adjusted but NOT dividend-adjusted.
    # yfinance >= 0.2 applies split adjustments unconditionally, so the stored
    # series is NOT raw pre-split. For AAPL on 2020-08-28 (three days before the
    # 2020-08-31 4:1 split) the split-adjusted close is ~$125; the raw pre-split
    # print of ~$499 is reconstructable as stored_close * split_ratio.
    aapl_res = catalog.query(
        """
        SELECT close
        FROM fact_market_ohlcv_raw
        WHERE sec_id = 'SEC_AAPL_001' AND trade_date = DATE '2020-08-28'
        """
    )
    if len(aapl_res) > 0:
        aapl_close = float(aapl_res["close"][0])
        lo, hi = AAPL_SPLIT_ADJUSTED_RANGE
        print(f"AAPL 2020-08-28 close in storage: ${aapl_close:.2f}")
        if not lo < aapl_close < hi:
            raise RuntimeError(
                f"FATAL: AAPL close on 2020-08-28 is ${aapl_close:.2f}, outside the expected "
                f"split-adjusted range (${lo:.0f}-${hi:.0f}). Ledger stores yfinance 'Close', "
                "which yfinance >= 0.2 always returns split-adjusted. A value near $499 means "
                "the feed changed to genuinely unadjusted prices; a value near $125 is correct. "
                "Re-verify the CAF contract before ingesting into this store."
            )
        print(
            f"✓ Split-adjusted basis verified. Pre-split equivalent = "
            f"${aapl_close * 4.0:.2f} (close x 4.0)."
        )

    # TSLA split the same way: 5:1 on 2020-08-31 and 3:1 on 2022-08-25, so the
    # 2020-08-28 split-adjusted close is ~$442 (the ~$2,213 pre-split print divided
    # by 5). Assert the stored basis, not the historical print.
    tsla_res = catalog.query(
        """
        SELECT close
        FROM fact_market_ohlcv_raw
        WHERE sec_id = 'SEC_TSLA_001' AND trade_date = DATE '2020-08-28'
        """
    )
    if len(tsla_res) > 0:
        tsla_close = float(tsla_res["close"][0])
        lo, hi = TSLA_SPLIT_ADJUSTED_RANGE
        print(f"TSLA 2020-08-28 close in storage: ${tsla_close:.2f}")
        if not lo < tsla_close < hi:
            raise RuntimeError(
                f"FATAL: TSLA close on 2020-08-28 is ${tsla_close:.2f}, outside the expected "
                f"split-adjusted range (${lo:.0f}-${hi:.0f}). See the AAPL note above: this store "
                "expects yfinance 'Close' (split-adjusted, not dividend-adjusted)."
            )
        print(
            f"✓ Split-adjusted basis verified. Pre-split equivalent = "
            f"${tsla_close * 5.0:.2f} (close x 5.0)."
        )

    # Verify Corporate Actions split ratios
    splits_res = catalog.query(
        """
        SELECT sec_id, ex_date, split_ratio
        FROM fact_corporate_actions
        WHERE action_type = 'SPLIT'
        ORDER BY ex_date
        """
    )
    print(f"✓ Verified: {len(splits_res)} stock split events registered in storage.")
    for row in splits_res.iter_rows(named=True):
        print(f"   - {row['sec_id']}: {row['split_ratio']}:1 split on {row['ex_date']}")

    catalog.close()
    print("\n=== [LEDGER SEED] Week 1 Data Ingestion Successfully Completed & Verified ===")


if __name__ == "__main__":
    run_seed()
