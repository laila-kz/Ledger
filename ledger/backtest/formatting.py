"""Fixed-width terminal formatting for comparative backtest reports."""

from __future__ import annotations

from .metrics import Metrics

METRIC_LABELS: tuple[tuple[str, str], ...] = (
    ("cumulative_return", "Cumulative Return"),
    ("cagr", "CAGR"),
    ("annualized_volatility", "Annualized Volatility"),
    ("sharpe_ratio", "Sharpe Ratio"),
    ("max_drawdown", "Max Drawdown"),
    ("calmar_ratio", "Calmar Ratio"),
    ("win_rate", "Win Rate"),
    ("profit_factor", "Profit Factor"),
    ("mean_turnover", "Mean Turnover"),
)


def render_comparison(
    leaky: Metrics,
    corrected: Metrics,
    delta: Metrics,
) -> str:
    """Render leaky, corrected, and corrected-minus-leaky metrics as ASCII."""
    rows = [
        (
            label,
            _format_metric(key, getattr(leaky, key)),
            _format_metric(key, getattr(corrected, key)),
            _format_metric(key, getattr(delta, key)),
        )
        for key, label in METRIC_LABELS
    ]
    widths = (max(len("Metric"), *(len(row[0]) for row in rows)), 12, 12, 12)
    separator = "-" * (sum(widths) + 9)
    lines = [
        (
            f"{'Metric':<{widths[0]}} | {'Leaky':>{widths[1]}} | "
            f"{'Corrected':>{widths[2]}} | {'Delta':>{widths[3]}}"
        ),
        separator,
    ]
    lines.extend(
        (
            f"{label:<{widths[0]}} | {leaky_value:>{widths[1]}} | "
            f"{corrected_value:>{widths[2]}} | {delta_value:>{widths[3]}}"
        )
        for label, leaky_value, corrected_value, delta_value in rows
    )
    lines.extend(
        [
            separator,
            "Delta convention: corrected - leaky. 'n/a' means the metric was undefined.",
        ]
    )
    return "\n".join(lines)


def _format_metric(name: str, value: float | None) -> str:
    if value is None:
        return "n/a"
    if name in {
        "cumulative_return",
        "cagr",
        "annualized_volatility",
        "max_drawdown",
        "win_rate",
        "mean_turnover",
    }:
        return f"{value:+.2%}"
    return f"{value:+.2f}"


__all__ = ["render_comparison"]
