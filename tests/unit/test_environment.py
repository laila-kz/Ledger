"""Basic environment and version verification test."""

import sys

import ledger


def test_python_version() -> None:
    """Verify python version is at least 3.10."""
    assert sys.version_info >= (3, 10), f"Expected Python >= 3.10, got {sys.version_info}"


def test_package_version() -> None:
    """Verify ledger package version."""
    assert ledger.__version__ == "0.1.0"
