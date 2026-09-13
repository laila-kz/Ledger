"""Tests for the Week 4 reference strategy."""

from datetime import datetime

import polars as pl
import pytest

from ledger.backtest.strategy import StrategyConfig, generate_target_weights


def test_momentum_selection_and_inverse_volatility_weights() -> None:
    features = pl.DataFrame(
        {
            "sec_id": ["A", "B", "C", "D"],
            "observation_timestamp": [datetime(2024, 1, 2, 21, 0)] * 4,
            "close": [110.0, 120.0, 90.0, 110.0],
            "momentum_20d": [0.30, 0.20, 0.90, None],
            "volatility_20d": [0.10, 0.20, 0.05, 0.10],
            "sma_50d": [100.0, 100.0, 100.0, 100.0],
        }
    )

    result = generate_target_weights(features, StrategyConfig(top_n=2))

    assert result.columns == ["observation_timestamp", "sec_id", "weight"]
    assert result.sort("sec_id")["weight"].to_list() == pytest.approx(
        [2.0 / 3.0, 1.0 / 3.0, 0.0, 0.0]
    )
    assert result["weight"].sum() == pytest.approx(1.0)


def test_missing_features_are_rejected() -> None:
    with pytest.raises(ValueError, match="volatility_20d"):
        generate_target_weights(
            pl.DataFrame(
                {
                    "sec_id": ["A"],
                    "observation_timestamp": [datetime(2024, 1, 2, 21, 0)],
                    "momentum_20d": [0.1],
                    "sma_50d": [100.0],
                    "close": [101.0],
                }
            )
        )
