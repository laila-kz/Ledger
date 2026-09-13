"""Subprocess integration test for the Day 5 CLI."""

import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl

UTC = timezone.utc


def test_cli_writes_manifest_curves_and_weights(tmp_path: Path) -> None:
    prices_path = tmp_path / "prices.parquet"
    rows: list[dict[str, object]] = []
    for offset in range(65):
        trade_date = date(2023, 1, 2) + timedelta(days=offset)
        known_from = datetime.combine(trade_date, datetime.min.time(), tzinfo=UTC).replace(
            hour=21
        )
        for sec_id, slope in (("SEC_A_001", 1.0), ("SEC_B_001", 0.5)):
            rows.append(
                {
                    "sec_id": sec_id,
                    "trade_date": trade_date,
                    "close": 100.0 + slope * offset,
                    "known_from": known_from,
                }
            )
    pl.DataFrame(rows).write_parquet(prices_path)
    artifacts_root = tmp_path / "artifacts" / "runs"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ledger.backtest.run_comparison",
            "--start-date",
            "2023-01-02",
            "--end-date",
            "2023-03-07",
            "--tickers",
            "A",
            "B",
            "--top-k",
            "1",
            "--prices-path",
            str(prices_path),
            "--preadjusted-prices-path",
            str(prices_path),
            "--artifacts-root",
            str(artifacts_root),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )

    assert completed.returncode == 0, completed.stderr
    assert "Cumulative Return" in completed.stdout
    run_id = next(
        line.split(": ", 1)[1]
        for line in completed.stdout.splitlines()
        if line.startswith("Run ID:")
    )
    run_dir = artifacts_root / run_id
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["run_id"] == run_id
    assert manifest["reproducible"]["inputs"][0]["sha256"].startswith("sha256:")
    for filename in (
        "equity_leaky.parquet",
        "equity_corrected.parquet",
        "returns_leaky.parquet",
        "returns_corrected.parquet",
        "weights_leaky.parquet",
        "weights_corrected.parquet",
    ):
        assert (run_dir / filename).is_file()
