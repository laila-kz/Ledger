"""Canary 07 – Synthetic demo sanity invariants.

What this canary is for
-----------------------
The demo's central claim is that point-in-time data access changes the
measured result. That claim is only worth anything if the two arms differ for
the reason we say they do.

An earlier version of this canary ran with *no* leak injected at all, so it was
asserting that the leaky arm outperformed the corrected arm on a path where
they were provably identical. It could only ever pass or fail by accident, and
when the arms were made genuinely identical it failed -- which is the correct
behaviour and the proof that the assertion was never vacuous.

So the leak is now injected deliberately and explicitly:

* ``leaky_fundamentals_view`` restates an amended EPS filing as if it were
  available at calendar-day start, so the control arm sees the amendment on
  the fiscal period end rather than on the later ``known_from`` stamp.
* ``require_eps_above=EPS_SCREEN_THRESHOLD`` places the screen between the
  original and amended EPS, so the control arm's eligibility -- and therefore
  its holdings -- depends on that premature information.

Both arms are then otherwise identical, and the assertions below check that
the injected leak shows up in the metrics in the expected direction.

Invariants asserted, with the leak present:
    1. leaky_sharpe   > corrected_sharpe  (leakage inflates risk-adjusted return)
    2. leaky_cumret   > corrected_cumret  (leakage inflates headline return)
    3. corrected_mdd  <= leaky_mdd        (leakage does not hide drawdown risk)
    4. leaky_sharpe   <= 5.0              (leakage inflates, but stays plausible)
    5. corrected_sharpe > 0.0             (the honest path is still tradable)

Plus one control test that removes the leak and requires the arms to become
identical, which is what makes invariants 1 and 2 mean anything.

NOTE: Numbers depend on PRNG_SEED in ledger/backtest/synthetic.py. If the
seed is changed, re-run this test to verify invariants still hold.
"""

from datetime import date, datetime, timezone

import polars as pl
import pytest

from ledger.backtest.metrics import TRADING_DAYS_PER_YEAR, compute_metrics
from ledger.backtest.runner import run_comparison
from ledger.backtest.simulation import SimulationConfig
from ledger.backtest.strategy import StrategyConfig
from ledger.backtest.synthetic import (
    EPS_SCREEN_THRESHOLD,
    OBSERVATION_TIME,
    generate_synthetic_data,
    leaky_fundamentals_view,
)

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Parameters matching the CLI demo invocation
# ---------------------------------------------------------------------------
_START = date(2020, 1, 1)
_END = date(2023, 12, 31)
_SEC_IDS = (
    "SEC_AAPL_001",
    "SEC_MSFT_001",
    "SEC_NVDA_001",
    "SEC_META_001",
    "SEC_GOOGL_001",
)
_TICKERS = tuple(s.split("_")[1] for s in _SEC_IDS)

# Leakage inflates measured performance; it should not inflate it without
# bound. A Sharpe in the double digits would mean the control arm is being
# handed something other than a modest filing-availability advantage.
MAX_PLAUSIBLE_LEAKY_SHARPE = 5.0


def _build_observations(raw_prices: pl.DataFrame) -> pl.DataFrame:
    dates = raw_prices.select("trade_date").unique().sort("trade_date")["trade_date"].to_list()
    timestamps = [datetime.combine(d, OBSERVATION_TIME, tzinfo=UTC) for d in dates]
    return pl.DataFrame(
        {
            "sec_id": [s for s in _SEC_IDS for _ in timestamps],
            "observation_timestamp": timestamps * len(_SEC_IDS),
        }
    )


def _run(*, inject_leak: bool):
    raw_prices, preadjusted, splits, fundamentals = generate_synthetic_data(_SEC_IDS, _START, _END)
    observations = _build_observations(raw_prices)

    kwargs: dict = {}
    if inject_leak:
        # Both arms receive the same fundamentals frame; only the availability
        # rule differs. The screen is shared, so both arms must have the
        # metric column the eligibility filter reads.
        kwargs = {
            "leaky_feature_views": (leaky_fundamentals_view(fundamentals),),
            "corrected_feature_views": (fundamentals,),
            "strategy_config": StrategyConfig(top_n=3, require_eps_above=EPS_SCREEN_THRESHOLD),
        }
    else:
        kwargs = {"strategy_config": StrategyConfig(top_n=3)}

    return run_comparison(
        observation_matrix=observations,
        raw_prices=raw_prices,
        preadjusted_prices=preadjusted,
        splits=splits,
        simulation_config=SimulationConfig(transaction_cost_bps=5.0),
        leaky_universe=_SEC_IDS,
        **kwargs,
    )


@pytest.fixture(scope="module")
def comparison_result():
    """Full synthetic demo comparison with the restatement leak injected."""
    return _run(inject_leak=True)


@pytest.fixture(scope="module")
def no_leak_result():
    """Same fixture with no leak injected: the controlled baseline."""
    return _run(inject_leak=False)


@pytest.fixture(scope="module")
def leaky_metrics(comparison_result):
    return compute_metrics(
        comparison_result.leaky.simulation, periods_per_year=TRADING_DAYS_PER_YEAR
    )


@pytest.fixture(scope="module")
def corrected_metrics(comparison_result):
    return compute_metrics(
        comparison_result.corrected.simulation, periods_per_year=TRADING_DAYS_PER_YEAR
    )


# ---------------------------------------------------------------------------
# Invariant 0: the canary is not vacuous
# ---------------------------------------------------------------------------
def test_removing_the_leak_makes_the_arms_identical(no_leak_result):
    """Without the injected leak the two arms must produce identical metrics.

    This is the control that gives invariants 1-2 their meaning. If the arms
    diverge here, the divergence in the leaky run is coming from something
    other than the injected filing-availability advantage, and every
    directional assertion below is measuring a confound.
    """
    leaky = compute_metrics(no_leak_result.leaky.simulation, periods_per_year=TRADING_DAYS_PER_YEAR)
    corrected = compute_metrics(
        no_leak_result.corrected.simulation, periods_per_year=TRADING_DAYS_PER_YEAR
    )

    for name in ("cumulative_return", "sharpe_ratio", "max_drawdown"):
        got = getattr(leaky, name)
        want = getattr(corrected, name)
        assert got == pytest.approx(want, abs=1e-12), (
            f"Arms diverge on {name} with no leak injected: leaky={got}, "
            f"corrected={want}. The canary is not a controlled experiment."
        )

    lw = no_leak_result.leaky.weights.sort(["observation_timestamp", "sec_id"])
    cw = no_leak_result.corrected.weights.sort(["observation_timestamp", "sec_id"])
    joined = lw.join(cw, on=["observation_timestamp", "sec_id"], how="full", suffix="_corrected")
    differing = joined.filter((pl.col("weight") - pl.col("weight_corrected")).abs() > 1e-12).height
    assert differing == 0, (
        f"{differing}/{joined.height} target-weight rows differ with no leak "
        "injected. Holdings must match exactly for this to be a control."
    )


# ---------------------------------------------------------------------------
# Invariant 1: leakage inflates Sharpe
# ---------------------------------------------------------------------------
def test_leaky_sharpe_exceeds_corrected(leaky_metrics, corrected_metrics):
    """The leaky pipeline must report a *higher* Sharpe than the corrected one.

    The control arm is handed the amended EPS as of the fiscal period end
    rather than its ``known_from`` stamp, and the screen sits between the two
    figures, so the control arm's eligibility depends on information it could
    not have had.
    """
    leaky_s = leaky_metrics.sharpe_ratio
    corrected_s = corrected_metrics.sharpe_ratio
    assert leaky_s is not None, "Leaky Sharpe must be computable"
    assert corrected_s is not None, "Corrected Sharpe must be computable"
    assert leaky_s > corrected_s, (
        f"Leakage should inflate Sharpe: leaky={leaky_s:.4f}, corrected={corrected_s:.4f}"
    )


# ---------------------------------------------------------------------------
# Invariant 2: leakage inflates cumulative return
# ---------------------------------------------------------------------------
def test_leaky_cumret_exceeds_corrected(leaky_metrics, corrected_metrics):
    """The leaky pipeline must report a *higher* cumulative return."""
    leaky_r = leaky_metrics.cumulative_return
    corrected_r = corrected_metrics.cumulative_return
    assert leaky_r is not None, "Leaky cumulative return must be computable"
    assert corrected_r is not None, "Corrected cumulative return must be computable"
    assert leaky_r > corrected_r, (
        f"Leakage should inflate return: leaky={leaky_r:.4f}, corrected={corrected_r:.4f}"
    )


# ---------------------------------------------------------------------------
# Invariant 3: leakage does not hide drawdown
# ---------------------------------------------------------------------------
def test_leaky_drawdown_not_worse_than_corrected(leaky_metrics, corrected_metrics):
    """The control arm must not report a *deeper* drawdown than the honest arm.

    Drawdown is negative; smaller (more negative) means worse. Leakage hides
    dips by construction, so the honest arm may legitimately look equal or
    worse.

    On the current fixture the restatement leak moves eligibility but not the
    drawdown path, so this holds with equality. It is kept as a guard: a
    corrected arm that came out *better* on drawdown would mean the control is
    being handed something that flatters it, not a leak.
    """
    leaky_mdd = leaky_metrics.max_drawdown
    corrected_mdd = corrected_metrics.max_drawdown
    assert leaky_mdd is not None, "Leaky MDD must be computable"
    assert corrected_mdd is not None, "Corrected MDD must be computable"
    # Compared with a tolerance: this leak leaves the drawdown path unchanged,
    # so the two agree to floating-point rounding rather than by construction.
    assert corrected_mdd <= leaky_mdd + 1e-12, (
        f"Corrected pipeline should not show a shallower drawdown than the "
        f"control: leaky={leaky_mdd:.4f}, corrected={corrected_mdd:.4f}"
    )


# ---------------------------------------------------------------------------
# Invariant 4: the inflation stays plausible
# ---------------------------------------------------------------------------
def test_leaky_sharpe_stays_plausible(leaky_metrics):
    """Leakage inflates performance; it should not produce fantasy numbers."""
    leaky_s = leaky_metrics.sharpe_ratio
    assert leaky_s is not None, "Leaky Sharpe must be computable"
    assert leaky_s <= MAX_PLAUSIBLE_LEAKY_SHARPE, (
        f"Leaky Sharpe {leaky_s:.4f} exceeds the plausibility bound "
        f"{MAX_PLAUSIBLE_LEAKY_SHARPE}. A modest filing-availability "
        "advantage cannot justify this."
    )


# ---------------------------------------------------------------------------
# Invariant 5: corrected pipeline is economically meaningful
# ---------------------------------------------------------------------------
def test_corrected_sharpe_is_positive(corrected_metrics):
    """The corrected pipeline must have a positive Sharpe over the 4-year window.

    This validates that the GBM path (with fixed seed) leaves the honest arm
    with a tradable result, so the demo is comparing two working strategies
    rather than a working strategy against a broken one. If this fails, change
    the seed and re-document.
    """
    corrected_s = corrected_metrics.sharpe_ratio
    assert corrected_s is not None, "Corrected Sharpe must be computable"
    assert corrected_s > 0.0, (
        f"Corrected Sharpe should be positive (strategy profitable on this path): "
        f"got {corrected_s:.4f}"
    )
