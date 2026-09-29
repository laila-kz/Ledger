# Archived Documents

These files are historical records, not current documentation. They are kept
for provenance — they show what was believed when, and the reasoning behind
decisions that were later reversed — but **nothing here should be treated as a
statement of current state.**

## Files

| File | What it was | Status |
| :--- | :--- | :--- |
| [`FIX_PLAN_SUPERSEDED.md`](FIX_PLAN_SUPERSEDED.md) | A point-in-time audit of the repository, with a remediation matrix of severities and fixes. | **Superseded.** Most rows are resolved. At least one was marked fixed when it was only partly fixed — see the Polars warning note below. |
| [`WAR_LOG.md`](WAR_LOG.md) | A day-by-day development log spanning Weeks 1–5. | **Historical.** Entries describe what was true when written. Counts and claims in it are deliberately not corrected. |

## Why they are here and not at the repository root

Both files were sitting at the root looking like current documentation. A
reviewer opening the repository would reasonably read `WAR_LOG.md` as a
description of the system. It is not: it says the canary suite has 10 tests
when it has 16, that `ledger lint` is "coming Day 4" when it is implemented,
and that the manifest is "Cryptographic" when it is unsigned.

Rewriting those entries to be accurate would falsify the record. Deleting them
would lose the reasoning. So they are archived with this note, and the current
authoritative state lives in:

- [`docs/CLAIMS.md`](../CLAIMS.md) — every quantitative and capability claim,
  with the command that verifies it
- [`CHANGELOG.md`](../../CHANGELOG.md) — what changed, and what it means
- [`README.md`](../../README.md) — how to use the project

## Corrections applied to the archived `FIX_PLAN.md`

Two edits were made in place, because they were wrong about the *present*, not
about the past:

1. The Polars sortedness-warning row was marked "✅ Verified Fixed". It is not
   fully fixed. The `filterwarnings` rule lives in
   `[tool.pytest.ini_options]`, so it applies only under pytest; running
   `python benchmarks/scale_probe.py` still emits the warning at
   `ledger/features/engine.py:310`. The row now says partially fixed and shows
   both commands side by side. See the note beneath the table in that file.

2. The row's original wording claimed the warning was silenced "across all test
   executions and CLI runs". The second half was never true.

Other rows were left as written. Several remain genuinely open, and they are
tracked in `docs/CLAIMS.md` §9 rather than here, because `CLAIMS.md` is the file
that gets kept current.

## A note on `WAR_LOG.md`

The log is left byte-for-byte as written, including claims that are now false.
This is deliberate. A development log is a record of what was known and believed
at a point in time; correcting it would make it a worse record, not a better
one. If you are looking for current state, you are in the wrong file.
