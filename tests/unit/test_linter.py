"""Unit tests for static AST leakage detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from ledger.commands.lint import main
from ledger.tools.linter import lint_source


@pytest.mark.parametrize(
    ("leaky", "compliant"),
    [
        ("prices.shift(-1)", "prices.shift(1)"),
        ("prices[-1]", "prices[1]"),
        ("prices.mean()", "prices.rolling(20).mean()"),
        ("prices.std()", "prices.rolling(20).std()"),
        ("StandardScaler().fit(prices)", "StandardScaler().fit(train_prices)"),
        ("prices.ffill()", "prices.ffill(limit=3)"),
        (
            "filings.join(prices, on='filing_date')",
            "filings.join(prices, on='known_from')",
        ),
    ],
)
def test_each_rule_has_leaky_and_compliant_cases(leaky: str, compliant: str) -> None:
    assert lint_source(leaky)
    assert not lint_source(compliant)


def test_rule_codes_cover_all_four_patterns() -> None:
    source = """\
prices.shift(-1)
prices.mean()
prices.ffill()
filings.join(prices, on='filing_date')
"""

    assert {finding.rule for finding in lint_source(source)} == {
        "L001",
        "L002",
        "L003",
        "L004",
    }


def test_lint_command_reports_location_snippet_and_advice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "strategy.py"
    script.write_text("signal = prices.shift(-1)\n", encoding="utf-8")

    exit_code = main([str(script)])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "L001" in output
    assert f"{script}:1:" in output
    assert "prices.shift(-1)" in output
    assert "Advice:" in output


def test_lint_command_passes_compliant_script(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "strategy.py"
    script.write_text("signal = prices.rolling(20).mean()\n", encoding="utf-8")

    assert main([str(script)]) == 0
    assert "PASS" in capsys.readouterr().out


def test_lint_command_rejects_syntax_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "broken.py"
    script.write_text("signal = prices.\n", encoding="utf-8")

    assert main([str(script)]) == 1
    assert "syntax error" in capsys.readouterr().out
