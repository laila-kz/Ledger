"""Sample quantitative strategy script for demonstration and testing of Ledger static linting.

This file intentionally contains both compliant and non-compliant time-series patterns
to showcase how `ledger lint` catches lookahead bias before execution.
"""

from __future__ import annotations

import pandas as pd


def compute_compliant_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compliant feature engineering using causal rolling windows and positive shifts."""
    out = df.copy()

    # Rule 1 compliant: Positive shift (lagging data backwards into the past)
    out["prev_close"] = out["close"].shift(1)

    # Rule 2 compliant: Rolling window calculation (no full-sample leakage)
    out["rolling_mean_20"] = out["close"].rolling(window=20).mean()
    out["rolling_std_20"] = out["close"].rolling(window=20).std()

    # Rule 3 compliant: Bounded forward fill with limit
    out["clean_volume"] = out["volume"].ffill(limit=5)

    return out


def compute_leaky_features(df: pd.DataFrame) -> pd.DataFrame:
    """Leaky feature engineering containing lookahead antipatterns."""
    out = df.copy()

    # Rule 1 Violation: Negative shift peeks into tomorrow's price (lookahead bias)
    out["future_close"] = out["close"].shift(-1)

    # Rule 2 Violation: Full-sample normalization computes global mean over all future periods
    out["normalized_close"] = (out["close"] - out["close"].mean()) / out["close"].std()

    # Rule 3 Violation: Unbounded forward fill across potentially restated intervals
    out["unbounded_fill"] = out["close"].ffill()

    return out
