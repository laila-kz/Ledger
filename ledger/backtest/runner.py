"""Two-pipeline vectorized backtest runner.

The runner keeps data sourcing, strategy execution, and return simulation
separate. Both pipelines receive the same observation matrix and simulation
configuration; only their feature and price inputs differ.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import polars as pl
import yfinance as yf

from ledger.features.engine import compute_features_as_of, join_features_as_of

from .simulation import SimulationConfig, simulate_portfolio
from .strategy import StrategyConfig, generate_target_weights

DEFAULT_FEATURE_NAMES = (
    "adj_close",
    "momentum_20d",
    "volatility_20d",
    "sma_50d",
)
LEAKY_STATIC_UNIVERSE = ("AAPL", "MSFT", "NVDA", "META", "GOOGL")


@dataclass(frozen=True)
class PipelineResult:
    """Features, target weights, and simulated returns for one pipeline."""

    features: pl.DataFrame
    weights_history: pl.DataFrame
    simulation: pl.DataFrame

    @property
    def weights(self) -> pl.DataFrame:
        """Backward-compatible alias for the canonical weights history."""
        return self.weights_history


@dataclass(frozen=True)
class ComparisonResult:
    """Results from the leaky and corrected pipelines."""

    leaky: PipelineResult
    corrected: PipelineResult


def build_corrected_features(
    observation_matrix: pl.DataFrame,
    raw_prices: pl.DataFrame,
    splits: pl.DataFrame | None = None,
    feature_names: tuple[str, ...] = DEFAULT_FEATURE_NAMES,
    additional_feature_views: tuple[pl.DataFrame, ...] = (),
) -> pl.DataFrame:
    """Build corrected features through the Ledger point-in-time engine.

    Any ``additional_feature_views`` (fundamentals, filings) are joined with
    ``join_features_as_of`` so that a record is only visible once it is actually
    known at the observation timestamp.
    """
    features = compute_features_as_of(
        entity_df=observation_matrix,
        feature_names=feature_names,
        prices=raw_prices,
        splits=splits,
    )
    if not additional_feature_views:
        return features
    return join_features_as_of(features, additional_feature_views)


def build_leaky_features(
    observation_matrix: pl.DataFrame,
    preadjusted_prices: pl.DataFrame,
    feature_names: tuple[str, ...] = DEFAULT_FEATURE_NAMES,
    additional_feature_views: tuple[pl.DataFrame, ...] = (),
    static_universe: tuple[str, ...] = LEAKY_STATIC_UNIVERSE,
) -> pl.DataFrame:
    """Build intentionally leaky features for the control pipeline.

    Preadjusted prices are treated as available at the start of their calendar
    day, and optional filing views are likewise moved to day-start availability.
    This models the control pipeline's same-day look-ahead. The observation
    matrix is filtered to the modern survivor-biased static universe. The
    universe may be overridden for deterministic tests or custom controls.
    """
    universe = _normalise_universe(static_universe)
    leaky_observations = observation_matrix.filter(pl.col("sec_id").is_in(universe))
    prices = _prepare_leaky_prices(preadjusted_prices)
    features = compute_features_as_of(
        entity_df=leaky_observations,
        feature_names=feature_names,
        prices=prices,
        splits=None,
    )
    if not additional_feature_views:
        return features

    leaky_views = tuple(_prepare_leaky_view(view) for view in additional_feature_views)
    return join_features_as_of(features, leaky_views)


def run_comparison(
    observation_matrix: pl.DataFrame,
    raw_prices: pl.DataFrame,
    preadjusted_prices: pl.DataFrame,
    splits: pl.DataFrame | None = None,
    strategy_config: StrategyConfig | None = None,
    simulation_config: SimulationConfig | None = None,
    feature_names: tuple[str, ...] = DEFAULT_FEATURE_NAMES,
    leaky_feature_views: tuple[pl.DataFrame, ...] = (),
    corrected_feature_views: tuple[pl.DataFrame, ...] = (),
    leaky_universe: tuple[str, ...] = LEAKY_STATIC_UNIVERSE,
    raw_prices_are_preadjusted: bool = False,
) -> ComparisonResult:
    """Run identical strategy/simulation settings against both data pipelines.

    ``leaky_feature_views`` are the same raw views handed to the control arm,
    which stamps them as available at calendar-day start. ``corrected_feature_views``
    are joined point-in-time. Passing the same frame to both is the honest way to
    isolate the leak: the only difference is the availability rule.

    ``raw_prices_are_preadjusted`` declares the price basis of ``raw_prices``.
    When the supplied levels already embed every corporate action, as a vendor
    ``Close``/``auto_adjust`` feed does, the ex-date ratio is already reflected
    in them and must not be applied again, so the flag switches the split
    adjustment off. Leave it ``False`` for genuinely unadjusted prints.

    This repository's own seeded catalog is as-traded, not pre-adjusted, so the
    default is correct for it. The flag exists for callers supplying a vendor
    series directly.
    """
    strategy = strategy_config or StrategyConfig()
    simulation = simulation_config or SimulationConfig()
    corrected_splits = None if raw_prices_are_preadjusted else splits

    leaky_features = build_leaky_features(
        observation_matrix=observation_matrix,
        preadjusted_prices=preadjusted_prices,
        feature_names=feature_names,
        additional_feature_views=leaky_feature_views,
        static_universe=leaky_universe,
    )
    corrected_features = build_corrected_features(
        observation_matrix=observation_matrix,
        raw_prices=raw_prices,
        splits=splits,
        feature_names=feature_names,
        additional_feature_views=corrected_feature_views,
    )

    leaky_weights = generate_target_weights(leaky_features, strategy)
    corrected_weights = generate_target_weights(corrected_features, strategy)

    leaky_result = PipelineResult(
        features=leaky_features,
        weights_history=leaky_weights,
        # No `splits` here on purpose: the control arm consumes vendor
        # pre-adjusted levels, which already reflect every corporate action and
        # are therefore continuous across an ex-date. Applying the ex-date ratio
        # on top of an already-adjusted series would count the split twice.
        simulation=simulate_portfolio(
            leaky_weights,
            _simulation_prices(leaky_features),
            simulation,
        ),
    )
    corrected_result = PipelineResult(
        features=corrected_features,
        weights_history=corrected_weights,
        # The point-in-time arm stores as-traded levels, so its price series
        # steps at the ex-date and `splits` restores economic neutrality there.
        # It marks to `adj_close` because the feature layer already applies the
        # PIT CAF to that column, and the ex-date step is the single
        # discontinuity the ratio is there to cancel.
        simulation=simulate_portfolio(
            corrected_weights,
            _simulation_prices(corrected_features),
            simulation,
            corrected_splits,
        ),
    )
    return ComparisonResult(leaky=leaky_result, corrected=corrected_result)


def load_yfinance_preadjusted_prices(
    tickers: list[str] | tuple[str, ...],
    start_date: str | date,
    end_date: str | date | None = None,
) -> pl.DataFrame:
    """Download the leaky control prices with yfinance ``auto_adjust=True``.

    This function is the only network-facing piece of the Day 2 runner. The
    resulting frame is deliberately marked available at calendar-day start by
    :func:`build_leaky_features`.
    """
    start = start_date.isoformat() if isinstance(start_date, date) else str(start_date)
    end = end_date.isoformat() if isinstance(end_date, date) else end_date
    rows: list[dict[str, Any]] = []
    for ticker in tickers:
        clean_ticker = ticker.strip().upper()
        history = yf.Ticker(clean_ticker).history(
            start=start,
            end=end,
            auto_adjust=True,
            actions=False,
        )
        if history.empty:
            continue
        for timestamp, row in history.iterrows():
            trade_date = timestamp.date() if hasattr(timestamp, "date") else timestamp
            rows.append(
                {
                    "sec_id": f"SEC_{clean_ticker}_001",
                    "trade_date": trade_date,
                    "close": float(row["Close"]),
                }
            )
    return _leaky_price_schema(rows)


def _prepare_leaky_prices(prices: pl.DataFrame) -> pl.DataFrame:
    required = {"sec_id", "trade_date", "close"}
    missing = sorted(required - set(prices.columns))
    if missing:
        raise ValueError(f"preadjusted_prices missing required columns: {missing}")
    result = prices
    if "adj_close" not in result.columns:
        result = result.with_columns(pl.col("close").alias("adj_close"))
    return result.with_columns(
        pl.col("trade_date").cast(pl.Datetime("us")).dt.replace_time_zone("UTC").alias("known_from")
    )


def _prepare_leaky_view(view: pl.DataFrame) -> pl.DataFrame:
    if "known_from" not in view.columns:
        raise ValueError("Leaky feature views must contain a known_from column.")
    return view.with_columns(pl.col("known_from").dt.truncate("1d").alias("known_from"))


def _simulation_prices(
    features: pl.DataFrame,
    price_column: str = "adj_close",
) -> pl.DataFrame:
    """Select the price series the simulator marks to market.

    The two arms need different columns. The leaky control consumes vendor
    ``auto_adjust`` levels, which are continuous across an ex-date, so it marks
    to ``adj_close``. The point-in-time arm stores as-traded prints and corrects
    the ex-date step with the split ratio, so it must mark to the raw
    ``close``: pairing the ratio with an already-CAF-adjusted series would apply
    the same split twice.
    """
    required = {"observation_timestamp", "sec_id", price_column}
    missing = sorted(required - set(features.columns))
    if missing:
        raise ValueError(f"Feature output cannot supply simulation prices: {missing}")
    return features.select(
        [
            "observation_timestamp",
            "sec_id",
            pl.col(price_column).alias("price"),
        ]
    ).drop_nulls("price")


def _normalise_universe(universe: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        value if value.upper().startswith("SEC_") else f"SEC_{value.strip().upper()}_001"
        for value in universe
    )


def _leaky_price_schema(rows: list[dict[str, Any]]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(
            schema={
                "sec_id": pl.String,
                "trade_date": pl.Date,
                "close": pl.Float64,
            }
        )
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


__all__ = [
    "ComparisonResult",
    "DEFAULT_FEATURE_NAMES",
    "LEAKY_STATIC_UNIVERSE",
    "PipelineResult",
    "build_corrected_features",
    "build_leaky_features",
    "load_yfinance_preadjusted_prices",
    "run_comparison",
]
