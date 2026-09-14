"""Hash-verified reproducibility manifests for backtest runs."""

from __future__ import annotations

import hashlib
import inspect
import json
import platform
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

import ledger
from ledger.features.registry import get_global_registry

MANIFEST_FILENAME = "manifest.json"
LOCKFILE_CANDIDATES = ("uv.lock", "poetry.lock", "requirements.txt")


def sha256_file(path: Path | str) -> str:
    """Return a prefixed SHA-256 digest for a file."""
    hasher = hashlib.sha256()
    with Path(path).open("rb") as file_handle:
        while chunk := file_handle.read(1024 * 1024):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def snapshot_input_files(
    paths: Sequence[Path | str],
    repo_root: Path | str = ".",
) -> list[dict[str, Any]]:
    """Hash input files before a backtest starts.

    Directory inputs are expanded recursively to sorted Parquet files. The
    returned snapshot is safe to pass unchanged into :func:`build_manifest`.
    """
    root = Path(repo_root).resolve()
    files = _expand_files(paths)
    return [_file_metadata(path, root) for path in files]


def build_manifest(
    *,
    repo_root: Path | str = ".",
    input_files: Sequence[Path | str] = (),
    input_snapshot: Sequence[Mapping[str, Any]] | None = None,
    feature_names: Sequence[str] = (),
    parameters: Mapping[str, Any] | None = None,
    results: Mapping[str, Any] | None = None,
    output_directory: Path | str | None = None,
    run_timestamp_utc: datetime | None = None,
) -> dict[str, Any]:
    """Build a manifest with a stable run ID derived from reproducible data."""
    root = Path(repo_root).resolve()
    inputs = (
        [dict(entry) for entry in input_snapshot]
        if input_snapshot is not None
        else snapshot_input_files(input_files, root)
    )
    lockfile = _lockfile_metadata(root)
    reproducible: dict[str, Any] = {
        "ledger_version": ledger.__version__,
        "python_version": platform.python_version(),
        "platform": f"{platform.system()}-{platform.machine()}",
        "git_commit_sha": _git_value(root, "rev-parse", "HEAD"),
        "lockfile": lockfile,
        "inputs": inputs,
        "features": _feature_metadata(feature_names, root),
        "parameters": _jsonable(parameters or {}),
        "results": _jsonable(results or {}),
    }
    run_id = _run_id(reproducible)
    timestamp = run_timestamp_utc or datetime.now(timezone.utc)
    audit: dict[str, Any] = {
        "run_timestamp_utc": _format_timestamp(timestamp),
        "git_branch": _git_value(root, "branch", "--show-current"),
        "git_is_dirty": _git_is_dirty(root),
    }
    if output_directory is not None:
        audit["output_directory"] = str(Path(output_directory))
    return {"run_id": run_id, "audit": audit, "reproducible": reproducible}


def write_manifest(
    manifest: Mapping[str, Any],
    artifacts_root: Path | str = "artifacts/runs",
) -> Path:
    """Write a canonical manifest to ``artifacts/runs/<run_id>/manifest.json``."""
    run_id = str(manifest["run_id"])
    output_dir = Path(artifacts_root) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / MANIFEST_FILENAME
    canonical = json.dumps(manifest, sort_keys=True, indent=2) + "\n"
    output_path.write_text(canonical, encoding="utf-8")
    return output_path


def _feature_metadata(feature_names: Sequence[str], repo_root: Path) -> list[dict[str, Any]]:
    if not feature_names:
        return []
    import importlib

    importlib.import_module("ledger.features.definitions")
    registry = get_global_registry()
    definitions = registry.resolve_execution_order(feature_names)
    metadata: list[dict[str, Any]] = []
    for definition in definitions:
        source_path = inspect.getsourcefile(definition.compute_fn)
        source_entry: dict[str, Any] = {
            "feature_name": definition.name,
            "version": definition.version,
            "definition_hash": definition.compute_hash(),
        }
        if source_path is not None:
            source_file = Path(source_path).resolve()
            source_entry["source_path"] = _relative_path(source_file, repo_root)
            source_entry["source_sha256"] = sha256_file(source_file)
        metadata.append(source_entry)
    return metadata


def _lockfile_metadata(repo_root: Path) -> dict[str, Any]:
    for candidate in LOCKFILE_CANDIDATES:
        path = repo_root / candidate
        if path.is_file():
            return {
                "path": _relative_path(path, repo_root),
                "sha256": sha256_file(path),
            }
    return {"path": None, "sha256": None}


def _file_metadata(path: Path, repo_root: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "path": _relative_path(path, repo_root),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }
    if path.suffix.lower() == ".parquet":
        try:
            metadata["row_count"] = int(pl.scan_parquet(path).select(pl.len()).collect().item())
        except Exception:
            metadata["row_count"] = None
    return metadata


def _expand_files(paths: Sequence[Path | str]) -> list[Path]:
    expanded: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            expanded.extend(
                sorted(candidate for candidate in path.rglob("*.parquet") if candidate.is_file())
            )
        elif path.is_file():
            expanded.append(path)
        else:
            raise FileNotFoundError(f"Manifest input path does not exist: {path}")
    unique = {path.resolve() for path in expanded}
    return sorted(unique, key=lambda value: str(value))


def _relative_path(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _run_id(reproducible: Mapping[str, Any]) -> str:
    canonical = json.dumps(reproducible, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _format_timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _git_value(repo_root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _git_is_dirty(repo_root: Path) -> bool | None:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return bool(completed.stdout.strip())


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


__all__ = [
    "MANIFEST_FILENAME",
    "build_manifest",
    "sha256_file",
    "snapshot_input_files",
    "write_manifest",
]
