"""Tests for deterministic hash-verified manifests."""

from datetime import datetime, timezone

import polars as pl

from ledger.backtest.metrics import Metrics
from ledger.lineage.manifest import (
    build_manifest,
    sha256_file,
    snapshot_input_files,
    verify_manifest,
    write_manifest,
)

UTC = timezone.utc


def test_manifest_hashes_inputs_features_and_is_timestamp_independent(tmp_path) -> None:
    input_path = tmp_path / "fact_market_ohlcv_raw.parquet"
    pl.DataFrame({"sec_id": ["SEC_A_001"], "close": [100.0]}).write_parquet(input_path)
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("lock-version = 1\n", encoding="utf-8")

    snapshot = snapshot_input_files([input_path], repo_root=tmp_path)
    first = build_manifest(
        repo_root=tmp_path,
        input_snapshot=snapshot,
        feature_names=["momentum_20d"],
        parameters={"top_k": 3, "cost_bps": 5},
        results={"leaky": Metrics(0.1, 0.05, None, None, -0.1, 0.5, 0.5, 1.2, 0.1)},
        run_timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = build_manifest(
        repo_root=tmp_path,
        input_snapshot=snapshot,
        feature_names=["momentum_20d"],
        parameters={"top_k": 3, "cost_bps": 5},
        results={"leaky": Metrics(0.1, 0.05, None, None, -0.1, 0.5, 0.5, 1.2, 0.1)},
        run_timestamp_utc=datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert first["run_id"] == second["run_id"]
    assert first["audit"]["run_timestamp_utc"] != second["audit"]["run_timestamp_utc"]
    assert first["reproducible"]["inputs"][0]["row_count"] == 1
    assert first["reproducible"]["lockfile"]["sha256"] == sha256_file(lockfile)
    assert first["reproducible"]["features"][0]["feature_name"] == "adj_close"


def test_write_manifest_uses_canonical_json(tmp_path) -> None:
    manifest = build_manifest(
        repo_root=tmp_path,
        run_timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
    )

    path = write_manifest(manifest, tmp_path / "artifacts" / "runs")
    text = path.read_text(encoding="utf-8")

    assert path.name == "manifest.json"
    assert text.endswith("\n")
    assert text.index('"audit"') < text.index('"reproducible"')
    assert '"run_id"' in text


def test_verify_manifest_passes_on_intact_data(tmp_path) -> None:
    """Verify that an intact manifest passes all checks."""
    input_path = tmp_path / "fact_market_ohlcv_raw.parquet"
    pl.DataFrame({"sec_id": ["SEC_A_001"], "close": [100.0]}).write_parquet(input_path)
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("lock-version = 1\n", encoding="utf-8")

    snapshot = snapshot_input_files([input_path], repo_root=tmp_path)
    manifest = build_manifest(
        repo_root=tmp_path,
        input_snapshot=snapshot,
        feature_names=["momentum_20d"],
        run_timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
    )

    manifest_path = write_manifest(manifest, tmp_path / "artifacts" / "runs")
    result = verify_manifest(manifest_path, repo_root=tmp_path)

    assert result.passed
    # Should have checks for: input file, feature, lockfile, git commit, run_id
    assert len(result.checks) >= 3
    assert all(passed for _, passed, _ in result.checks)


def test_verify_manifest_detects_input_file_tampering(tmp_path) -> None:
    """Verify that modifying an input file is detected."""
    input_path = tmp_path / "fact_market_ohlcv_raw.parquet"
    pl.DataFrame({"sec_id": ["SEC_A_001"], "close": [100.0]}).write_parquet(input_path)
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("lock-version = 1\n", encoding="utf-8")

    snapshot = snapshot_input_files([input_path], repo_root=tmp_path)
    manifest = build_manifest(
        repo_root=tmp_path,
        input_snapshot=snapshot,
        run_timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
    )
    manifest_path = write_manifest(manifest, tmp_path / "artifacts" / "runs")

    # Tamper with input file
    pl.DataFrame({"sec_id": ["SEC_A_001", "SEC_B_001"], "close": [100.0, 200.0]}).write_parquet(
        input_path
    )

    result = verify_manifest(manifest_path, repo_root=tmp_path)
    assert not result.passed

    # Find the tampering check
    tampering_checks = [
        error
        for name, passed, error in result.checks
        if "Input file" in name and not passed
    ]
    assert len(tampering_checks) > 0
    assert "Hash mismatch" in tampering_checks[0]


def test_verify_manifest_detects_missing_input_file(tmp_path) -> None:
    """Verify that a missing input file is detected."""
    input_path = tmp_path / "fact_market_ohlcv_raw.parquet"
    pl.DataFrame({"sec_id": ["SEC_A_001"], "close": [100.0]}).write_parquet(input_path)
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("lock-version = 1\n", encoding="utf-8")

    snapshot = snapshot_input_files([input_path], repo_root=tmp_path)
    manifest = build_manifest(
        repo_root=tmp_path,
        input_snapshot=snapshot,
        run_timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
    )
    manifest_path = write_manifest(manifest, tmp_path / "artifacts" / "runs")

    # Delete the input file
    input_path.unlink()

    result = verify_manifest(manifest_path, repo_root=tmp_path)
    assert not result.passed

    missing_checks = [
        error for name, passed, error in result.checks if "Input file" in name and not passed
    ]
    assert len(missing_checks) > 0
    assert "File not found" in missing_checks[0]


def test_verify_manifest_detects_lockfile_tampering(tmp_path) -> None:
    """Verify that modifying a lockfile is detected."""
    input_path = tmp_path / "fact_market_ohlcv_raw.parquet"
    pl.DataFrame({"sec_id": ["SEC_A_001"], "close": [100.0]}).write_parquet(input_path)
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("lock-version = 1\n", encoding="utf-8")

    snapshot = snapshot_input_files([input_path], repo_root=tmp_path)
    manifest = build_manifest(
        repo_root=tmp_path,
        input_snapshot=snapshot,
        run_timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
    )
    manifest_path = write_manifest(manifest, tmp_path / "artifacts" / "runs")

    # Tamper with lockfile
    lockfile.write_text("lock-version = 2\n# modified\n", encoding="utf-8")

    result = verify_manifest(manifest_path, repo_root=tmp_path)
    assert not result.passed

    lockfile_checks = [
        error for name, passed, error in result.checks if "Lockfile" in name and not passed
    ]
    assert len(lockfile_checks) > 0
    assert "Hash mismatch" in lockfile_checks[0]


def test_verify_manifest_result_formatting(tmp_path) -> None:
    """Verify that verification results are formatted for human readability."""
    input_path = tmp_path / "fact_market_ohlcv_raw.parquet"
    pl.DataFrame({"sec_id": ["SEC_A_001"], "close": [100.0]}).write_parquet(input_path)
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("lock-version = 1\n", encoding="utf-8")

    snapshot = snapshot_input_files([input_path], repo_root=tmp_path)
    manifest = build_manifest(
        repo_root=tmp_path,
        input_snapshot=snapshot,
        run_timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
    )
    manifest_path = write_manifest(manifest, tmp_path / "artifacts" / "runs")

    result = verify_manifest(manifest_path, repo_root=tmp_path)
    output = str(result)

    # Should contain title and status
    assert "Manifest Verification Results" in output
    assert "Overall: PASSED ✓" in output
    assert "✓ PASS" in output


def test_verify_manifest_formatting_on_failure(tmp_path) -> None:
    """Verify that failed verification shows clear error messages."""
    input_path = tmp_path / "fact_market_ohlcv_raw.parquet"
    pl.DataFrame({"sec_id": ["SEC_A_001"], "close": [100.0]}).write_parquet(input_path)
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("lock-version = 1\n", encoding="utf-8")

    snapshot = snapshot_input_files([input_path], repo_root=tmp_path)
    manifest = build_manifest(
        repo_root=tmp_path,
        input_snapshot=snapshot,
        run_timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
    )
    manifest_path = write_manifest(manifest, tmp_path / "artifacts" / "runs")

    # Tamper with input
    pl.DataFrame({"sec_id": ["SEC_B_001"], "close": [200.0]}).write_parquet(input_path)

    result = verify_manifest(manifest_path, repo_root=tmp_path)
    output = str(result)

    # Should show failure status
    assert "Overall: FAILED ✗" in output
    assert "✗ FAIL" in output
    assert "Hash mismatch" in output

