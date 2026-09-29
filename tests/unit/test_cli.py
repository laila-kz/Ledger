"""Reachability tests for the Week 5 console command contract."""

import pytest

from ledger.cli import main


def test_top_level_help_is_reachable(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "run-comparison" in output
    assert "verify-manifest" in output


def test_version_is_reachable(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"


def test_subcommand_help_is_reachable(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["lint"]) == 0
    assert "Static leakage" in capsys.readouterr().out

    assert main(["verify-manifest"]) == 0
    assert "Verify a Ledger" in capsys.readouterr().out

    assert main(["canaries", "--help"]) == 0
    assert "Leakage Canary" in capsys.readouterr().out

    assert main(["run-comparison", "--help"]) == 0
    assert "--start-date" in capsys.readouterr().out
