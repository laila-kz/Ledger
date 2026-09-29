"""Unit tests for vectorized portfolio return simulation."""

from datetime import date, datetime

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
    assert result["cost"].to_list() == pytest.approx([0.0005, second_turnover * 0.0005])
    assert result["equity"][-1] == pytest.approx((1.10 - 0.0005) * (1.0 - second_turnover * 0.0005))


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


def _split_scenario() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Single security: 100 -> 400 on the ex-date, a 4:1 split.

    The raw print drops 4x on the ex-date while the holder's share count
    multiplies by 4, so wealth is unchanged.
    """
    timestamps = [datetime(2024, 1, 2, 21, 30), datetime(2024, 1, 3, 21, 30)]
    weights = pl.DataFrame(
        {
            "observation_timestamp": timestamps,
            "sec_id": ["A", "A"],
            "weight": [1.0, 1.0],
        }
    )
    prices = pl.DataFrame(
        {
            "observation_timestamp": timestamps,
            "sec_id": ["A", "A"],
            "price": [100.0, 25.0],
        }
    )
    splits = pl.DataFrame(
        {
            "sec_id": ["A"],
            "ex_date": [date(2024, 1, 3)],
            "split_ratio": [4.0],
            "known_from": [datetime(2024, 1, 3, 21, 15)],
        }
    )
    return weights, prices, splits


def test_split_is_economically_neutral_when_supplied() -> None:
    """A split must not move the portfolio when the level series is raw.

    The level print falls 75% on the ex-date. A holder's wealth does not, so
    the simulated return must be flat rather than -75%.
    """
    weights, prices, splits = _split_scenario()

    result = simulate_portfolio(weights, prices, SimulationConfig(transaction_cost_bps=0.0), splits)

    assert result["gross_return"].to_list() == pytest.approx([0.0])
    assert result["equity"].to_list() == pytest.approx([1.0])


def test_split_is_ignored_when_not_supplied() -> None:
    """Omitting ``splits`` keeps naive level returns.

    That is the correct behaviour for a continuously pre-adjusted series, where
    the levels never step and applying the ratio would double-count.
    """
    weights, prices, _splits = _split_scenario()

    result = simulate_portfolio(weights, prices, SimulationConfig(transaction_cost_bps=0.0))

    assert result["gross_return"].to_list() == pytest.approx([25.0 / 100.0 - 1.0])


def test_split_not_yet_known_is_not_applied() -> None:
    """A split announced after the return is realised must not be used.

    The ratio is only knowable once announced, so applying it before the
    announcement would be look-ahead in the one place the simulation is
    otherwise strictly backward-looking.
    """
    weights, prices, splits = _split_scenario()
    late = splits.with_columns(pl.lit(datetime(2024, 1, 4, 21, 15)).alias("known_from"))

    result = simulate_portfolio(weights, prices, SimulationConfig(transaction_cost_bps=0.0), late)

    assert result["gross_return"].to_list() == pytest.approx([25.0 / 100.0 - 1.0])


def test_duplicate_split_rows_do_not_compound_the_ratio() -> None:
    """Re-ingested corporate-action rows must stay idempotent.

    An append-only catalog that has been seeded more than once holds several
    byte-identical copies of one event. Aggregating the ex-date ratio across
    those copies would turn a single 4:1 split into 4**8 = 65,536 and book a
    ~6,500% single-period return.
    """
    weights, prices, splits = _split_scenario()
    duplicated = pl.concat([splits] * 8, how="vertical")

    result = simulate_portfolio(
        weights, prices, SimulationConfig(transaction_cost_bps=0.0), duplicated
    )

    assert result["gross_return"].to_list() == pytest.approx([0.0])


def test_distinct_splits_on_one_ex_date_still_compound() -> None:
    """Deduplication must not collapse genuinely separate events.

    Two real splits effective on the same date for one security are distinct
    rows and their ratios are meant to multiply.
    """
    weights, prices, splits = _split_scenario()
    second = splits.with_columns(pl.lit(2.0).alias("split_ratio"))
    both = pl.concat([splits, second], how="vertical")

    result = simulate_portfolio(weights, prices, SimulationConfig(transaction_cost_bps=0.0), both)

    # 4:1 and 2:1 on the same day: (25/100) * 4 * 2 - 1 = 1.0
    assert result["gross_return"].to_list() == pytest.approx([1.0])
