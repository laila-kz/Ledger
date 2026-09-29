"""Labelled corpus for the static leakage linter, used to measure precision/recall.

Why this exists
---------------
`tests/unit/test_linter.py` asserts that each rule fires on a leaky snippet and
stays quiet on a compliant one. That proves the rules are *reachable*. It says
nothing about whether they are *accurate* -- a linter that flags every line of a
correct strategy passes every one of those tests.

So this module carries a labelled corpus instead: snippets with a declared
verdict per rule. `test_linter_corpus.py` runs the real linter over it and
computes, per rule:

* **precision** -- of the findings the rule emitted, how many were labelled
  leaky. Low precision means developers learn to ignore the rule.
* **recall** -- of the snippets genuinely exhibiting the pattern, how many the
  rule caught. Low recall means a real leak ships.

Labelling convention
--------------------
A snippet is labelled per rule with a frozenset of rule codes that *should*
fire on it. An empty set means the snippet is a true negative for every rule.
A snippet may legitimately be a true positive for one rule and a true negative
for the others, which is exactly what makes per-rule measurement meaningful
instead of a single blended number.

The false-positive set is the interesting half. Several entries are patterns
that are only *superficially* the ones the rules name -- `equity[-1]` is a
final-value read, not a lookahead; `shift(-1).over(...)` is the LEAD() idiom
used to derive interval upper bounds, not a peek at the future. If the linter
flags those, that is a measured false positive, recorded here rather than
quietly deleted.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CorpusCase:
    """One labelled snippet.

    Attributes:
        name: Stable identifier, used in failure messages.
        source: The Python source to lint.
        leaky_rules: Rule codes that should fire. Empty means a true negative.
        note: Why the case is labelled the way it is.
    """

    name: str
    source: str
    leaky_rules: frozenset[str]
    note: str


# ---------------------------------------------------------------------------
# True positives: each pattern the rules are meant to catch.
# ---------------------------------------------------------------------------

TRUE_POSITIVES: tuple[CorpusCase, ...] = (
    CorpusCase(
        name="l001_shift_negative",
        source="future = prices.shift(-1)",
        leaky_rules=frozenset({"L001"}),
        note="Classic lookahead: tomorrow's price on today's row.",
    ),
    CorpusCase(
        name="l001_shift_negative_keyword",
        source="future = prices.shift(periods=-1)",
        leaky_rules=frozenset({"L001"}),
        note=(
            "Negative offset passed by keyword rather than position. The rule "
            "reads node.args[0] only, so it misses this. Recorded as a known "
            "false negative rather than exempted, since the pattern is a real "
            "leak the rule should catch."
        ),
    ),
    CorpusCase(
        name="l001_slice_negative_bound",
        source="future = prices[0:-1]",
        leaky_rules=frozenset({"L001"}),
        note="Negative slice bound on a time series.",
    ),
    CorpusCase(
        name="l001_numpy_negative_index",
        source="future = arr[-1]",
        leaky_rules=frozenset({"L001"}),
        note="Negative positional index on an array-like.",
    ),
    CorpusCase(
        name="l002_full_sample_mean",
        source="z = (prices - prices.mean()) / prices.std()",
        leaky_rules=frozenset({"L002"}),
        note="Full-sample normalisation: mean and std both see every period.",
    ),
    CorpusCase(
        name="l002_unwindowed_mean",
        source="avg = volume.mean()",
        leaky_rules=frozenset({"L002"}),
        note="Unwindowed mean over the whole sample.",
    ),
    CorpusCase(
        name="l002_scaler_fit_on_full_sample",
        source="scaled = StandardScaler().fit(prices).transform(prices)",
        leaky_rules=frozenset({"L002"}),
        note="Fitting a scaler on the full sample, not a training partition.",
    ),
    CorpusCase(
        name="l003_ffill_unbounded",
        source="filled = prices.ffill()",
        leaky_rules=frozenset({"L003"}),
        note="Unbounded forward fill carries values across restatement gaps.",
    ),
    CorpusCase(
        name="l004_join_on_filing_date",
        source="merged = filings.join(prices, on='filing_date')",
        leaky_rules=frozenset({"L004"}),
        note="Joining on filing_date exposes the row before it was public.",
    ),
    CorpusCase(
        name="l004_join_on_filing_date_list",
        source="merged = filings.join(prices, on=['sec_id', 'filing_date'])",
        leaky_rules=frozenset({"L004"}),
        note="Same leak via a composite key list.",
    ),
    CorpusCase(
        name="l004_left_on_filing_date",
        source="merged = prices.merge(filings, left_on='filing_date')",
        leaky_rules=frozenset({"L004"}),
        note="Same leak via merge/left_on spelling.",
    ),
    CorpusCase(
        name="l001_and_l002_together",
        source=("future = prices.shift(-1)\ncentered = prices - prices.mean()\n"),
        leaky_rules=frozenset({"L001", "L002"}),
        note="Multi-rule snippet: per-rule scoring must count each independently.",
    ),
)


# ---------------------------------------------------------------------------
# True negatives: correct strategies that must stay silent.
#
# Several of these are the patterns Ledger's own production code uses. If the
# linter flags them, the fix belongs in the rule, not in the caller.
# ---------------------------------------------------------------------------

TRUE_NEGATIVES: tuple[CorpusCase, ...] = (
    CorpusCase(
        name="tn_rolling_mean",
        source="avg = prices.rolling(20).mean()",
        leaky_rules=frozenset(),
        note="Trailing window: only past and present observations.",
    ),
    CorpusCase(
        name="tn_rolling_mean_std",
        source=("avg = prices.rolling(20).mean()\nvol = prices.rolling(20).std()\n"),
        leaky_rules=frozenset(),
        note="Both statistics windowed.",
    ),
    CorpusCase(
        name="tn_expanding_mean",
        source="avg = prices.expanding().mean()",
        leaky_rules=frozenset(),
        note="Expanding window is causal by construction.",
    ),
    CorpusCase(
        name="tn_ewm_mean",
        source="avg = prices.ewm(span=20).mean()",
        leaky_rules=frozenset(),
        note="Exponentially weighted window is causal.",
    ),
    CorpusCase(
        name="tn_scaler_fit_on_training",
        source="scaled = StandardScaler().fit(train_prices).transform(prices)",
        leaky_rules=frozenset(),
        note="Fitting on a training partition is the sanctioned pattern.",
    ),
    CorpusCase(
        name="tn_scaler_fit_training_suffix",
        source="scaled = MinMaxScaler().fit(prices_training).transform(prices)",
        leaky_rules=frozenset(),
        note="`_training` suffix is also recognised as a training partition.",
    ),
    CorpusCase(
        name="tn_ffill_limited",
        source="filled = prices.ffill(limit=3)",
        leaky_rules=frozenset(),
        note="Bounded fill cannot bridge an arbitrarily long gap.",
    ),
    CorpusCase(
        name="tn_ffill_limit_keyword",
        source="filled = prices.ffill(limit=3)",
        leaky_rules=frozenset(),
        note="Same bounded fill, exercised as a keyword argument.",
    ),
    CorpusCase(
        name="tn_join_on_known_from",
        source="merged = filings.join(prices, on='known_from')",
        leaky_rules=frozenset(),
        note="known_from is the point-in-time availability key.",
    ),
    CorpusCase(
        name="tn_join_on_sec_id",
        source="merged = prices.join(fundamentals, on='sec_id')",
        leaky_rules=frozenset(),
        note="Entity key alone carries no time semantics.",
    ),
    CorpusCase(
        name="tn_shift_positive",
        source="lagged = prices.shift(1)",
        leaky_rules=frozenset(),
        note="Positive shift is strictly backwards.",
    ),
    CorpusCase(
        name="tn_arithmetic_means",
        source="total = a.mean() + b.mean()\nratio = c.mean() / d.mean()",
        leaky_rules=frozenset(),
        note="Means of scalars/aggregates, not series-wide statistics.",
    ),
    # --- The measured false-positive candidates -----------------------------
    # These are patterns the rule *names* but which are legitimate in context.
    # Ledger's own production code contains the first two.
    CorpusCase(
        name="tn_final_value_equity",
        source="total_return = equity[-1] - 1.0",
        leaky_rules=frozenset(),
        note=(
            "Reads the final element of an already-computed equity curve. "
            "This is a post-hoc metric, not a per-row feature, so there is no "
            "future information to leak. Ledger's metrics.py does this."
        ),
    ),
    CorpusCase(
        name="tn_lead_for_interval_bounds",
        source=(
            "bounds = df.sort('sec_id').with_columns(\n"
            "    pl.col('known_from').shift(-1).over('sec_id').alias('known_to')\n"
            ")\n"
        ),
        leaky_rules=frozenset(),
        note=(
            "shift(-1) inside .over() is the polars/SQL LEAD() idiom used to "
            "derive interval upper bounds from append-only rows. Every row is "
            "already known when written, so this cannot leak. Ledger's "
            "bitemporal.py derives known_to and valid_to exactly this way."
        ),
    ),
    # NOTE: a LEAD() *without* a window is not treated as compliant. A bare
    # shift(-1) is syntactically identical whether it derives an interval bound
    # over already-materialised weights or peeks at a future price, and the
    # distinction is not available in the AST. Labelling it compliant would
    # teach the rule to ignore a real leak, so it stays a documented false
    # positive rather than an exemption.
    CorpusCase(
        name="tn_list_slicing_not_timeseries",
        source=("recent = history[-1]\nwindow = history[-20:]\n"),
        leaky_rules=frozenset(),
        note=(
            "Negative offsets on a plain Python list are ordinary 'most recent "
            "N' access. A list has no row-wise alignment, so the lookahead the "
            "rule guards against cannot occur here."
        ),
    ),
    CorpusCase(
        name="tn_string_slicing",
        source="suffix = ticker[-1]",
        leaky_rules=frozenset(),
        note="String indexing, unrelated to time-series alignment.",
    ),
    CorpusCase(
        name="tn_numpy_linalg_index",
        source="last = matrix[i, -1]",
        leaky_rules=frozenset(),
        note="Column selection in linear algebra, not a row-wise feature.",
    ),
)
