"""Integration tests for comparative tear-sheet output."""

from datetime import datetime, timedelta, timezone

import polars as pl

from ledger.backtest.tear_sheet import build_tear_sheet


def test_tear_sheet_reports_corrected_minus_leaky_delta() -> None:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    leaky = pl.DataFrame(
        {
            "observation_timestamp": [start + timedelta(days=i) for i in range(3)],
            "net_return": [0.10, 0.0, 0.10],
            "equity": [1.10, 1.10, 1.21],
            "turnover": [1.0, 0.1, 0.1],
        }
    )
    corrected = leaky.with_columns(
        pl.Series("net_return", [0.05, 0.0, 0.05]),
        pl.Series("equity", [1.05, 1.05, 1.1025]),
    )

    sheet = build_tear_sheet(leaky, corrected, periods_per_year=252)

    delta_return = sheet.delta.cumulative_return
    assert delta_return is not None, "delta cumulative return must be computable"
    assert delta_return < 0.0
    assert "Corrected" in sheet.render()
    assert "corrected - leaky" in sheet.render()
    assert "n/a" in sheet.render()
