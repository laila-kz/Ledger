"""Unit tests for the two-pipeline Day 2 runner."""

from datetime import date, datetime, timedelta, timezone

import polars as pl

from ledger.backtest.runner import run_comparison
from ledger.backtest.simulation import SimulationConfig
from ledger.backtest.strategy import StrategyConfig

UTC = timezone.utc


def _price_frame(num_days: int = 65) -> pl.DataFrame:
    base_date = date(2023, 1, 2)
    rows: list[dict[str, object]] = []
    for offset in range(num_days):
        trade_date = base_date + timedelta(days=offset)
        known_from = datetime.combine(trade_date, datetime.min.time(), tzinfo=UTC).replace(hour=21)
        for sec_id, slope in [("SEC_A_001", 1.0), ("SEC_B_001", 0.5)]:
            close = 100.0 + slope * offset
            rows.append(
                {
                    "sec_id": sec_id,
                    "trade_date": trade_date,
                    "close": close,
                    "known_from": known_from,
                }
            )
    return pl.DataFrame(rows)


def _observations() -> pl.DataFrame:
    timestamps = [
        datetime(2023, 2, 26, 21, 5, tzinfo=UTC),
        datetime(2023, 2, 27, 21, 5, tzinfo=UTC),
    ]
    return pl.DataFrame(
        {
            "sec_id": ["SEC_A_001"] * len(timestamps) + ["SEC_B_001"] * len(timestamps),
            "observation_timestamp": timestamps * 2,
        }
    )


def test_runner_executes_both_pipelines_with_shared_simulation() -> None:
    raw_prices = _price_frame()
    result = run_comparison(
        observation_matrix=_observations(),
        raw_prices=raw_prices,
        preadjusted_prices=raw_prices,
        simulation_config=SimulationConfig(transaction_cost_bps=5.0),
        leaky_universe=("SEC_A_001", "SEC_B_001"),
    )

    assert result.leaky.weights.columns == ["observation_timestamp", "sec_id", "weight"]
    assert result.corrected.weights.columns == ["observation_timestamp", "sec_id", "weight"]
    assert result.leaky.simulation.columns == [
        "observation_timestamp",
        "gross_return",
        "turnover",
        "cost",
        "net_return",
        "equity",
    ]
    assert result.leaky.simulation["observation_timestamp"].to_list() == (
        result.corrected.simulation["observation_timestamp"].to_list()
    )
    assert result.leaky.simulation["cost"][0] >= 0.0


def test_pipelines_diverge_before_split_is_known() -> None:
    base_date = date(2020, 7, 1)
    split_offset = 61
    split_date = base_date + timedelta(days=split_offset)
    rows: list[dict[str, object]] = []
    for offset in range(split_offset + 1):
        trade_date = base_date + timedelta(days=offset)
        known_from = datetime.combine(trade_date, datetime.min.time(), tzinfo=UTC).replace(hour=21)
        a_raw = (
            25.0 + 0.1 * offset if offset < split_offset else 100.0 + 0.1 * (offset - split_offset)
        )
        rows.extend(
            [
                {
                    "sec_id": "SEC_A_001",
                    "trade_date": trade_date,
                    "close": a_raw,
                    "known_from": known_from,
                },
                {
                    "sec_id": "SEC_B_001",
                    "trade_date": trade_date,
                    "close": 100.0 + 0.3 * offset,
                    "known_from": known_from,
                },
            ]
        )

    raw_prices = pl.DataFrame(rows)
    preadjusted = raw_prices.with_columns(
        pl.when((pl.col("sec_id") == "SEC_A_001") & (pl.col("trade_date") < split_date))
        .then(pl.col("close") * 4.0)
        .otherwise(pl.col("close"))
        .alias("close")
    )
    splits = pl.DataFrame(
        {
            "sec_id": ["SEC_A_001"],
            "ex_date": [split_date],
            "split_ratio": [4.0],
            "known_from": [
                datetime.combine(split_date, datetime.min.time(), tzinfo=UTC).replace(hour=23)
            ],
        }
    )
    observations = pl.DataFrame(
        {
            "sec_id": ["SEC_A_001", "SEC_B_001"],
            "observation_timestamp": [
                datetime.combine(split_date, datetime.min.time(), tzinfo=UTC).replace(
                    hour=21, minute=5
                )
            ]
            * 2,
        }
    )

    result = run_comparison(
        observation_matrix=observations,
        raw_prices=raw_prices,
        preadjusted_prices=preadjusted,
        splits=splits,
        strategy_config=StrategyConfig(top_n=1),
        leaky_universe=("SEC_A_001", "SEC_B_001"),
    )

    leaky_selected = result.leaky.weights.filter(pl.col("weight") > 0)["sec_id"].to_list()
    corrected_selected = result.corrected.weights.filter(pl.col("weight") > 0)["sec_id"].to_list()
    assert leaky_selected == ["SEC_B_001"]
    assert corrected_selected == ["SEC_A_001"]
