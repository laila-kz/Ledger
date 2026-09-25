# Blueprint: Formal Verification of Bitemporal Invariants
### TLA+ Model Checking + Property-Based Testing for the Ledger Core

**Domain:** Formal Methods / Distributed Systems Correctness, applied to Quantitative Data Engineering
**Extends:** `ledger/core/bitemporal.py`, `ledger/core/entity.py`
**Timeline:** 5–7 focused days
**Positioning:** This is the single module that moves Ledger from "well-tested data pipeline" to "formally-proven temporal system." Almost no portfolio project at any level attempts this — it is standard practice in distributed databases (CockroachDB, TigerBeetle) and safety-critical systems, essentially unseen in quant/DE portfolios.

---

## 1. Why This Module Exists

Every other part of Ledger validates correctness **empirically**: the 10 canaries assert that specific, hand-picked scenarios produce the right answer. That's necessary but not sufficient — example-based tests only prove the system is correct on the examples you thought to write. They cannot prove the *absence* of a whole class of bug: a bitemporal interval invariant silently violated by an input combination nobody anticipated (concurrent restatements arriving in the same millisecond, a corporate action with `known_from == valid_from`, an entity remap racing a filing).

This module adds two complementary techniques on top of the canaries, not instead of them:

1. **TLA+ model checking** — a mathematical specification of the bitemporal state machine, exhaustively explored by a model checker (TLC) across every reachable state up to a bound, proving invariants hold or producing a concrete counterexample trace if they don't.
2. **Property-based testing (Hypothesis)** — the same invariants, expressed as Python properties, fuzzed against the *actual implementation* (not the spec) with thousands of generated inputs, closing the gap between "the design is correct" and "the code matches the design."

The pitch to an interviewer: *"I didn't just test that the bitemporal engine handles the scenarios I thought of — I specified the invariants it must always satisfy, model-checked the design against them, and fuzzed the implementation against the same invariants."* That is a categorically different (and more senior) correctness claim than "174 tests passing."

---

## 2. What Exactly Gets Verified

Your bitemporal core (`ledger/core/bitemporal.py`, from Week 1/2 of the build) already encodes these invariants informally, in code comments and canary assertions. This module makes them **explicit, machine-checked properties**:

| Invariant | Informal statement | Currently enforced by |
|---|---|---|
| **NoOverlap** | For a given `(sec_id, metric_name, fiscal_period_end)`, no two rows' `[known_from, known_to)` intervals overlap | Implicit in `LEAD()` derivation — never directly asserted |
| **Monotonic** | `known_from < known_to` for every derived interval (or `known_to IS NULL` for the latest row) | Implicit in `ORDER BY known_from, ingestion_seq` |
| **NoGaps** | The derived `known_to` of row *n* always equals the `known_from` of row *n+1* for the same partition key — no interval "holes" | Not currently asserted anywhere |
| **ValidBeforeKnown** | A fact cannot become knowable before it becomes true: `known_from >= valid_from` (you can't learn about an event before it happens) | Not currently enforced at all — this is the most likely real bug |
| **EntityContinuity** | Across a ticker rebrand, `sec_id` resolution never produces a gap or a double-mapped interval in `v_bitemporal_ticker_map` | Covered only by Canary 06's single scenario |
| **IdempotentReplay** | Re-ingesting an already-seen `(sec_id, known_from, ingestion_seq)` row is a no-op, not a duplicate interval | Not currently tested |

`ValidBeforeKnown` is worth calling out specifically in your write-up and interview prep — it's a genuinely plausible bug in your current design (nothing in the Week 1 guide's `LEAD()` derivation logic stops a malformed input from having `known_from < valid_from`), and it's exactly the kind of thing a model checker finds and an example-based test suite doesn't, because you'd have to already suspect it to write a canary for it.

---

## 3. Architecture: Where This Sits in the Existing Repo

This is additive — it does not change `ledger/core/bitemporal.py`'s implementation, only adds a specification and a fuzz-testing layer that hold it accountable.

```
ledger/
├── core/
│   ├── bitemporal.py          # UNCHANGED — the implementation under verification
│   ├── entity.py               # UNCHANGED
│   └── calendars.py

docs/
├── formal/                     # NEW
│   ├── Ledger.tla               # TLA+ specification of the bitemporal state machine
│   ├── Ledger.cfg                # TLC model-checker config (constants, invariants to check)
│   ├── tlc_run_log.txt           # Captured output: states explored, invariants held
│   └── README.md                 # How to install TLA+ tools & re-run the model check
├── adr/
│   └── 0005-formal-verification-of-bitemporal-invariants.md   # NEW ADR

tests/
├── canaries/                   # UNCHANGED — the 10 existing scenario tests
├── unit/                       # UNCHANGED
└── property/                   # NEW
    ├── conftest.py               # Hypothesis strategies for generating corporate actions,
    │                             #   filings, and ingestion event sequences
    └── test_bitemporal_invariants.py   # The 6 properties above, as Hypothesis @given tests

.github/workflows/
└── ci.yml                      # EXTENDED — add a `pytest tests/property/` step
                                 #   (TLC model check stays a documented manual/optional step,
                                 #   not part of every CI run — see §6)
```

The key design decision: **the TLA+ spec and the Hypothesis tests check the same six invariants, but against two different things.** TLC checks that your *design* (the abstract state machine you wrote in TLA+) never violates them, across every state reachable within a bound. Hypothesis checks that your *Python implementation* never violates them, across thousands of randomly generated concrete inputs. Passing both is what lets you claim the design is sound **and** the code matches the design — either one alone is a weaker claim, and you should say so explicitly if asked.

---

## 4. Part A — The TLA+ Specification

### 4.1 What you're modeling

You are not modeling all of Ledger — you are modeling the **derivation state machine**: given a stream of ingested rows (each with `sec_id`, `known_from`, `valid_from`, `ingestion_seq`), how `known_to` and `valid_to` get derived, and what must always be true of the result. This is small and tractable, which is exactly why TLA+ is the right tool — you're not trying to model Polars or DuckDB, just the temporal logic.

### 4.2 Spec skeleton (`docs/formal/Ledger.tla`)

```tla
---------------------------- MODULE Ledger ----------------------------
EXTENDS Integers, Sequences, TLC

CONSTANTS
    SecIDs,          \* e.g. {"AAPL_ID", "TSLA_ID"} — small finite set for model checking
    MaxTime,         \* bound the timestamp domain, e.g. 0..10, to keep state space finite
    MaxEvents        \* bound how many ingestion events TLC explores, e.g. 4-6

VARIABLES
    ingested,        \* sequence of ingested rows so far: << [sec_id |-> ..., known_from |-> ..., 
                     \*    valid_from |-> ..., ingestion_seq |-> ...], ... >>
    nextSeq          \* monotonic ingestion_seq counter

vars == << ingested, nextSeq >>

\* --- Derivation: known_to for a row is the known_from of the NEXT row for the
\*     same sec_id, ordered by (known_from, ingestion_seq). NULL (modeled as -1)
\*     if it is the latest row.
KnownTo(row) ==
    LET laterRows == { r \in Range(ingested) :
                          r.sec_id = row.sec_id
                          /\ (r.known_from > row.known_from
                              \/ (r.known_from = row.known_from /\ r.ingestion_seq > row.ingestion_seq)) }
    IN IF laterRows = {} THEN -1
       ELSE LET minRow == CHOOSE r \in laterRows :
                              \A r2 \in laterRows : r.known_from <= r2.known_from
            IN minRow.known_from

\* --- The action: ingest one new row (append-only — never mutates 'ingested')
Ingest(sid, kf, vf) ==
    /\ Len(ingested) < MaxEvents
    /\ kf \in 0..MaxTime  /\ vf \in 0..MaxTime
    /\ ingested' = Append(ingested, [sec_id |-> sid, known_from |-> kf,
                                       valid_from |-> vf, ingestion_seq |-> nextSeq])
    /\ nextSeq' = nextSeq + 1

Next == \E sid \in SecIDs, kf, vf \in 0..MaxTime : Ingest(sid, kf, vf)

Init == ingested = << >> /\ nextSeq = 0

Spec == Init /\ [][Next]_vars

\* ============================ INVARIANTS ============================

\* NoOverlap: no two rows for the same sec_id have overlapping [known_from, known_to) intervals
NoOverlap ==
    \A i, j \in DOMAIN ingested :
        i /= j /\ ingested[i].sec_id = ingested[j].sec_id
        => ~Overlaps(Interval(ingested[i]), Interval(ingested[j]))

\* Monotonic: known_from is always strictly less than its derived known_to (or known_to is NULL)
Monotonic ==
    \A row \in Range(ingested) : KnownTo(row) = -1 \/ row.known_from < KnownTo(row)

\* ValidBeforeKnown: you cannot learn about a fact before it becomes true
ValidBeforeKnown ==
    \A row \in Range(ingested) : row.known_from >= row.valid_from

=============================================================================
```

*(This is a skeleton to build from, not a copy-paste-ready file — `Overlaps`, `Interval`, and `Range` are small helper operators you'll define, and the real spec needs to explicitly handle the `fiscal_period_end`/`metric_name` partition key your actual Parquet schema uses, not just `sec_id`. Budget a day of TLA+-specific ramp-up: the Lamport video course, sections 1–3, is the fastest path in.)*

### 4.3 Running the model checker

```bash
# TLC ships with the TLA+ Toolbox, or run headless via the tla2tools.jar CLI:
java -cp tla2tools.jar tlc2.TLC -config docs/formal/Ledger.cfg docs/formal/Ledger.tla
```

`Ledger.cfg` sets small finite constants (e.g. `SecIDs = {a, b}`, `MaxTime = 5`, `MaxEvents = 4`) — the state space grows combinatorially, so the whole discipline of model checking is choosing bounds small enough to exhaustively explore in minutes but large enough to actually exercise the interesting interleavings (in particular, two ingestion events for the same `sec_id` with `known_from` values that are equal, or one millisecond apart, are the cases that actually stress `NoOverlap`).

**Deliverable artifact:** capture the TLC output — `N states generated, M distinct states found, diameter of state graph K` and the final `Model checking completed. No error has been found.` — into `docs/formal/tlc_run_log.txt`. If TLC *does* find a counterexample (plausible for `ValidBeforeKnown`, given it's currently unenforced), that's not a failure of the exercise — that's the deliverable. Fix the spec or the design decision it exposes, document the fix, and keep the failing trace as an appendix. A discovered-and-fixed invariant violation is a substantially better interview story than a clean pass on the first try.

---

## 5. Part B — Property-Based Testing (Hypothesis)

This is where you close the loop between the abstract spec and your actual `ledger/core/bitemporal.py` implementation.

### 5.1 `tests/property/conftest.py` — strategies

```python
from hypothesis import strategies as st
from datetime import datetime, timedelta, timezone

# Generates a single synthetic ingestion event
@st.composite
def ingestion_event(draw):
    sec_id = draw(st.sampled_from(["SEC_AAPL", "SEC_TSLA", "SEC_META"]))
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    valid_from = draw(st.integers(min_value=0, max_value=1000))
    # Deliberately allow known_from to be generated BEFORE valid_from sometimes —
    # this is exactly the case that should be caught, not filtered out.
    known_offset = draw(st.integers(min_value=-50, max_value=500))
    return {
        "sec_id": sec_id,
        "valid_from": base + timedelta(days=valid_from),
        "known_from": base + timedelta(days=valid_from + known_offset),
    }

# A sequence of events, which will be fed to the ingestion pipeline IN THIS ORDER —
# used both for normal-order and, separately, permuted/shuffled tests
event_sequence = st.lists(ingestion_event(), min_size=1, max_size=12)
```

Deliberately generating invalid-looking inputs (like `known_offset` going negative) rather than constraining the strategy to only "nice" inputs is the whole point — Hypothesis's job is to find the input that breaks you, and constraining the generator to inputs you already believe are safe defeats that.

### 5.2 `tests/property/test_bitemporal_invariants.py` — the properties

```python
from hypothesis import given, settings, HealthCheck
from ledger.core.bitemporal import derive_known_to  # your actual implementation
from tests.property.conftest import event_sequence

@given(events=event_sequence)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=500)
def test_no_overlapping_intervals(events):
    result = derive_known_to(events)  # calls your real bitemporal derivation logic
    by_sec_id = group_by(result, key="sec_id")
    for sec_id, rows in by_sec_id.items():
        rows_sorted = sorted(rows, key=lambda r: (r["known_from"], r["ingestion_seq"]))
        for a, b in zip(rows_sorted, rows_sorted[1:]):
            assert a["known_to"] == b["known_from"], (
                f"Gap or overlap between {a} and {b}"
            )

@given(events=event_sequence)
def test_monotonic_intervals(events):
    result = derive_known_to(events)
    for row in result:
        if row["known_to"] is not None:
            assert row["known_from"] < row["known_to"]

@given(events=event_sequence)
def test_valid_before_known(events):
    """This is expected to be the interesting one — see if it currently fails."""
    result = derive_known_to(events)
    for row in result:
        assert row["known_from"] >= row["valid_from"], (
            f"Row claims to be known before it was valid: {row}"
        )

@given(events=event_sequence)
def test_idempotent_reingestion(events):
    result_once = derive_known_to(events)
    result_twice = derive_known_to(events + events)  # naive duplicate — should be a no-op
    assert normalize(result_once) == normalize(dedupe(result_twice))
```

`test_valid_before_known` is written to be run *first* against your actual code, before you've decided whether to enforce the invariant — the honest version of this exercise is running it, seeing whether it fails, and reporting the real result rather than writing tests you already know will pass.

### 5.3 Going further: stateful testing

Hypothesis's `RuleBasedStateMachine` lets you generate not just batches of events, but **sequences of ingestion operations interleaved with queries**, which is a closer match to your actual failure surface (a restatement arriving *between* two queries, not just a static batch). This is worth adding as a stretch extension once the basic properties are green — and it's the natural bridge to the chaos/adversarial-testing idea if you decide to build that module too, since `RuleBasedStateMachine` is exactly the tool for both.

---

## 6. Integration with Existing CI (`.github/workflows/ci.yml`)

Two different cadences, deliberately:

- **`pytest tests/property/`** — fast (Hypothesis with `max_examples=500` runs in seconds), deterministic-enough with a fixed seed, and belongs in every CI run alongside the existing canary suite and unit tests.
- **TLC model checking** — not something you re-run on every commit (it's a manual/exploratory tool, and larger bounds can take minutes), but you should document it as a **required step whenever `ledger/core/bitemporal.py`'s interval-derivation logic changes**, and keep the log file checked in as evidence. A good middle ground: add a `make verify-formal` target and mention it explicitly in your PR template / CONTRIBUTING notes as required before merging changes to `core/bitemporal.py` — this is exactly how real formal-methods-adjacent teams handle it (spec re-verification gated on touching the relevant module, not on every push).

```yaml
# Addition to existing ci.yml, alongside the current ruff/mypy/pytest steps
  - name: Run property-based invariant tests
    run: pytest tests/property/ -v --hypothesis-seed=42
```

---

## 7. Deliverables Checklist

- [ ] `docs/formal/Ledger.tla` — the specification
- [ ] `docs/formal/Ledger.cfg` — model-checker configuration
- [ ] `docs/formal/tlc_run_log.txt` — captured TLC output (states explored, invariants checked, pass/fail)
- [ ] `docs/formal/README.md` — one page: what's specified, how to install TLA+ tooling, how to re-run
- [ ] `tests/property/conftest.py` + `test_bitemporal_invariants.py` — the six Hypothesis properties
- [ ] CI step running the property tests on every push
- [ ] **`docs/adr/0005-formal-verification-of-bitemporal-invariants.md`** — new ADR explaining *why* TLA+ + property testing rather than just more example-based canaries (mirrors the structure of your existing ADR-0001 through 0004)
- [ ] README addition: a short "Formal Verification" section (mirroring the existing "Developer Tooling" section's style) pointing to the ADR and the `docs/formal/` directory
- [ ] If TLC or Hypothesis finds a real violation: a short writeup of the bug, the fix, and the before/after — this is your best single interview artifact from the whole exercise

---

## 8. Suggested Day-by-Day Plan

| Day | Focus |
|---|---|
| **1** | TLA+ fundamentals (Lamport video course sections 1–3, or the *Learn TLA+* site). Sketch the state machine on paper before writing any spec. |
| **2** | Write `Ledger.tla` — `Init`, `Next`, the derivation operators, and the first invariant (`NoOverlap`). Get TLC running on a trivial config. |
| **3** | Add `Monotonic`, `ValidBeforeKnown`, `NoGaps` invariants. Run TLC with increasing bounds; capture the log. Investigate and document any counterexamples. |
| **4** | Write the Hypothesis strategies and the four core property tests against the real `ledger/core/bitemporal.py`. Wire into CI. |
| **5** | If time allows: `RuleBasedStateMachine` stateful extension modeling interleaved ingest/query sequences. |
| **6** | Write ADR-0005, the `docs/formal/README.md`, and the README section. Rehearse explaining the TLA+ spec and the Hypothesis results out loud — this module lives or dies on how well you can defend it verbally in an interview, not on the code alone. |

---

## 9. Interview Defense — What You Need to Be Able to Explain Cold

1. **Why TLA+ and not just more pytest?** — TLA+ explores the *entire* bounded state space, not just the examples you thought to write; it proves absence of a bug class, not just presence of correctness on chosen inputs.
2. **What's the actual difference between the TLA+ check and the Hypothesis check?** — one verifies the design, the other verifies the implementation matches the design. Neither alone is sufficient.
3. **What are the bounds on TLC, and why does that matter?** — model checking within `MaxTime`/`MaxEvents` bounds is exhaustive *within those bounds*, not a proof for all possible inputs — be precise about this distinction, it's the first thing a rigorous interviewer will probe.
4. **Did you find a real bug?** — if yes, walk through it in detail; if no, be honest that a clean pass means "no violation within the explored bounds," not "provably bug-free," and explain what you'd do to increase confidence further (larger bounds, more invariants, symmetry reduction).

---

## 10. Resume Bullet (Final Form)

> Formally specified the bitemporal ingestion state machine in TLA+ and model-checked interval-overlap, monotonicity, and knowledge-causality invariants across the bounded reachable state space; cross-validated the production Python implementation against the same invariants via property-based testing (Hypothesis), integrated into CI.

Adjust the specific invariant list and the "found N bugs" clause once you've actually run it — don't pre-write a discovery you haven't made yet.
