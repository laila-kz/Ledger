"""Unit tests for vectorized portfolio return simulation."""

from datetime import datetime

import polars as pl
import pytest

from ledger.backtest.simulation import SimulationConfig, simulate_portfolio


def test_simulation_uses_drifted_weights_for_turnover() -> None:
    timestamps = [datetime(2024, 1, 1), datetime(2024, 1, 2), datetime(2024, 1, 3)]
    weights = pl.DataFrame(
        {
            "observation_timestamp": timestamps * 2,
            "sec_id": ["A"] * 3 + ["B"] * 3,
            "weight": [0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
        }
    )
    prices = pl.DataFrame(
        {
            "observation_timestamp": timestamps * 2,
            "sec_id": ["A"] * 3 + ["B"] * 3,
            "price": [100.0, 120.0, 120.0, 100.0, 100.0, 100.0],
        }
    )

    result = simulate_portfolio(weights, prices, SimulationConfig(transaction_cost_bps=5.0))

    assert result["gross_return"].to_list() == pytest.approx([0.10, 0.0])
    # After the first period, drifted weights are 120/220 and 100/220.
    assert result["turnover"].to_list() == pytest.approx(
        [1.0, abs(0.5 - 120.0 / 220.0) + abs(0.5 - 100.0 / 220.0)]
    )
    second_turnover = abs(0.5 - 120.0 / 220.0) + abs(0.5 - 100.0 / 220.0)
    assert result["cost"].to_list() == pytest.approx(
        [0.0005, second_turnover * 0.0005]
    )
    assert result["equity"][-1] == pytest.approx(
        (1.10 - 0.0005) * (1.0 - second_turnover * 0.0005)
    )


def test_simulation_rejects_duplicate_price_keys() -> None:
    prices = pl.DataFrame(
        {
            "observation_timestamp": [datetime(2024, 1, 1)] * 2,
            "sec_id": ["A", "A"],
            "price": [100.0, 101.0],
        }
    )
    weights = pl.DataFrame(
        {
            "observation_timestamp": [datetime(2024, 1, 1)],
            "sec_id": ["A"],
            "weight": [1.0],
        }
    )

    with pytest.raises(ValueError, match="prices must contain"):
        simulate_portfolio(weights, prices)
