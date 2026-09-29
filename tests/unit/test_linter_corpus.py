"""Measures the linter's precision and recall against the labelled corpus.

Run directly for a readable report:

    pytest tests/unit/test_linter_corpus.py -s -k report

The thresholds below are the contract. If a rule change pushes a rate below
its floor, this fails -- which is the point: a linter whose accuracy silently
degrades is worse than no linter, because it trains developers to ignore it.
"""

from __future__ import annotations

import sys
from collections import Counter

import pytest

from ledger.tools.linter import lint_source
from tests.corpus.linter_corpus import (
    TRUE_NEGATIVES,
    TRUE_POSITIVES,
    CorpusCase,
)

ALL_CASES: tuple[CorpusCase, ...] = TRUE_POSITIVES + TRUE_NEGATIVES
RULES: tuple[str, ...] = ("L001", "L002", "L003", "L004")

# Per-rule floors. Deliberately below 1.0: a static heuristic linter has
# irreducible error, and a threshold of 1.0 would only be met by deleting the
# rules. The point is that the error rate is measured, published, and cannot
# silently regress.
#
# The L001/L002 gaps are a known, irreducible limit of AST-only analysis. Given
# `equity[-1]`, no amount of syntax inspection distinguishes a final-value read
# on a materialised list from a peek at the last bar of a Series; both are a
# bare `Name` under a negative index. Same for `a.mean()` where `a` may be a
# scalar aggregate or a full-sample series. Exempting them needs a name
# allowlist, which would be a guess that rots silently the first time a series
# is called something else -- strictly worse than an honest measured rate.
#
# Measured today: L001 5 TP / 3 FP (62.5% precision, 100% recall), L002
# 4/1 (80%, 100%), L003 and L004 both perfect. The three L001 false positives
# are bare `Name` subscripts and a string index -- `equity[-1]`, `history[-20:]`,
# `ticker[-1]` -- where the container's type is what decides the answer and the
# AST does not carry types. Every one of the three is idiomatic Python that a
# maintainer would have to silence before trusting the rule.
#
# A type-aware linter would resolve both. That is the real fix, and it is out of
# scope here; these floors record where the static linter actually stands.
MIN_PRECISION: dict[str, float] = {"L001": 0.60, "L002": 0.75, "L003": 0.90, "L004": 0.90}
MIN_RECALL: dict[str, float] = {"L001": 0.80, "L002": 0.90, "L003": 0.90, "L004": 0.90}


def _emitted(case: CorpusCase) -> frozenset[str]:
    return frozenset(finding.rule for finding in lint_source(case.source))


def _measure() -> dict[str, Counter[str]]:
    """Return per-rule TP/FP/FN counters over the whole corpus."""
    counts: dict[str, Counter[str]] = {rule: Counter() for rule in RULES}
    for case in ALL_CASES:
        emitted = _emitted(case)
        for rule in RULES:
            should = rule in case.leaky_rules
            did = rule in emitted
            if should and did:
                counts[rule]["tp"] += 1
            elif did and not should:
                counts[rule]["fp"] += 1
            elif should and not did:
                counts[rule]["fn"] += 1
    return counts


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


MEASURED = _measure()


@pytest.mark.parametrize("rule", RULES)
def test_rule_recall_meets_floor(rule: str) -> None:
    """Every genuinely leaky snippet is still caught."""
    counts = MEASURED[rule]
    missed = [c.name for c in ALL_CASES if rule in c.leaky_rules and rule not in _emitted(c)]
    recall = _rate(counts["tp"], counts["tp"] + counts["fn"])
    assert recall >= MIN_RECALL[rule], (
        f"{rule} recall {recall:.2%} is below the {MIN_RECALL[rule]:.0%} floor "
        f"({counts['fn']} missed: {missed}). A rule that stops catching real "
        "leaks is a correctness regression, not a tuning preference."
    )


@pytest.mark.parametrize("rule", RULES)
def test_rule_precision_meets_floor(rule: str) -> None:
    """The rule does not fire on correct code often enough to teach ignoring."""
    counts = MEASURED[rule]
    noisy = [c.name for c in ALL_CASES if rule not in c.leaky_rules and rule in _emitted(c)]
    precision = _rate(counts["tp"], counts["tp"] + counts["fp"])
    assert precision >= MIN_PRECISION[rule], (
        f"{rule} precision {precision:.2%} is below the {MIN_PRECISION[rule]:.0%} floor "
        f"({counts['fp']} false positives: {noisy}). Fix the rule, not the corpus."
    )


def test_corpus_covers_every_rule() -> None:
    """A rule with no labelled cases would report a vacuous 100%."""
    for rule in RULES:
        positives = [case for case in ALL_CASES if rule in case.leaky_rules]
        assert positives, f"{rule} has no true-positive corpus case"


def test_corpus_has_negative_coverage() -> None:
    """Precision is only meaningful if the corpus contains true negatives."""
    assert len(TRUE_NEGATIVES) >= len(TRUE_POSITIVES), (
        "A linter corpus needs at least as many compliant snippets as leaky "
        "ones, or the measured precision is optimistic by construction."
    )


def test_report(capsys: pytest.CaptureFixture[str]) -> None:
    """Publish the measured rates. No threshold; always passes."""
    lines = [
        "Linter accuracy over labelled corpus",
        f"  cases: {len(TRUE_POSITIVES)} leaky / {len(TRUE_NEGATIVES)} compliant",
        "",
        f"  {'rule':<6} {'TP':>3} {'FP':>3} {'FN':>3} {'precision':>10} {'recall':>8}",
    ]
    for rule in RULES:
        counts = MEASURED[rule]
        precision = _rate(counts["tp"], counts["tp"] + counts["fp"])
        recall = _rate(counts["tp"], counts["tp"] + counts["fn"])
        lines.append(
            f"  {rule:<6} {counts['tp']:>3} {counts['fp']:>3} {counts['fn']:>3} "
            f"{precision:>9.1%} {recall:>8.1%}"
        )
    print("\n".join(lines), file=sys.stderr)
    capsys.readouterr()
