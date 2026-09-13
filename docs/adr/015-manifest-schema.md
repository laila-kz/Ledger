# ADR 015: Reproducible Run Manifest Schema

## Status
Accepted - recorded 2026-09-13 (Week 4 Day 4)

## Decision
Manifests separate stable reproducibility content from changing audit metadata.
The `run_id` is the first 16 hexadecimal characters of the SHA-256 hash of the
canonical JSON encoding of `reproducible`.

The manifest contains:

- `reproducible`: package/Python/platform metadata, Git commit, lockfile hash,
  input file hashes and row counts, feature definition/source hashes, run
  parameters, and tear-sheet results.
- `audit`: UTC timestamp, branch, dirty-tree status, and output directory.
- `run_id`: stable identifier derived only from `reproducible`.

JSON is written with `sort_keys=True`, two-space indentation, and a trailing
newline. Input snapshots can be computed before a backtest with
`snapshot_input_files()` and passed to `build_manifest()` so the manifest
describes the files actually consumed.

## Consequences
Changing timestamps or audit context does not change `run_id`. Changing any
input bytes, feature source/hash, dependency lockfile, parameters, or results
does. Missing lockfiles are represented explicitly as null metadata rather than
silently omitted.