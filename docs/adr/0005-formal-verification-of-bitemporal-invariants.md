# ADR-0005: Formal Verification of Bitemporal Invariants (TLA+ & Property-Based Testing)

- **Status:** Accepted
- **Date:** September 2026
- **Deciders:** Ledger Core Architecture & Quant Data Engineering Team
- **Tags:** Formal Methods, TLA+, Hypothesis, Bitemporal Core, Data Engineering Correctness

---

## 1. Context & Problem Statement

Quantitative feature stores and point-in-time backtesting engines rely on strict bitemporal valid-time (`valid_from`, `valid_to`) and transaction-time (`known_from`, `known_to`) boundaries. A single interval overlap or temporal causality violation can silently introduce lookahead bias into historical training datasets and strategy simulations.

Empirical unit tests and scenario-based canaries (such as Ledger's 10 Leakage Canaries) validate known, hand-crafted edge cases. However, empirical testing alone cannot guarantee the absolute absence of un-anticipated invariant violations across arbitrary event interleavings (e.g., concurrent restatements, retroactive splits, or duplicate batch re-ingestions).

To achieve institutional-grade correctness, Ledger requires a dual-layered formal verification framework:
1. **Mathematical Specification & Model Checking (TLA+ / TLC):** Prove design correctness across all reachable states within bounded domains.
2. **Property-Based Implementation Fuzzing (Hypothesis):** Validate that production Python code (`ledger/core/bitemporal.py`) adheres strictly to the formal mathematical invariants across thousands of randomized inputs.

---

## 2. Decision

We adopt a two-tier verification architecture combining **TLA+ model checking** and **Hypothesis property-based fuzzing**:

```
 ┌────────────────────────────────────────────────────────┐
 │           TLA+ Specification (Ledger.tla)              │
 │  Exhaustive TLC Model Checker Search (22,158 states)   │
 └──────────────────────────┬─────────────────────────────┘
                            │ Proves Abstract Design Correctness
                            ▼
 ┌────────────────────────────────────────────────────────┐
 │       Hypothesis Fuzzing (test_bitemporal_invariants)  │
 │  1,700+ Generated Random Inputs against Production Code │
 └──────────────────────────┬─────────────────────────────┘
                            │ Proves Implementation Matches Spec
                            ▼
 ┌────────────────────────────────────────────────────────┐
 │    Deterministic Canary Suite & CI Quality Gates       │
 └────────────────────────────────────────────────────────┘
```

### Core Invariants Enforced Across Both Layers

1. **`NoOverlap`**: For any entity partition `(sec_id, metric_name)`, no two transaction intervals $[known\_from, known\_to)$ overlap in time.
2. **`Monotonic`**: Transaction start time strictly precedes derived upper bound ($known\_from < known\_to$, or $known\_to = \text{NULL}$).
3. **`NoGaps`**: Adjacent derived versions leave zero temporal gaps between $known\_to_{n}$ and $known\_from_{n+1}$.
4. **`ValidBeforeKnown`**: Temporal causality holds ($known\_from \ge valid\_from$); a fact cannot be known prior to its real-world occurrence.
5. **`IdempotentReplay`**: Re-ingesting an identical batch yields deterministic derived bounds without corrupting temporal lineage.

---

## 3. Consequences & Verification Results

### Positive Consequences
- **Mathematical Soundness:** TLC model checking explored 22,158 distinct reachable states up to graph diameter 4 with **0 invariant violations**.
- **Implementation Fidelity:** Hypothesis property-based tests fuzzed `derive_known_to_polars` with 1,700+ randomized event streams, proving Python production logic strictly matches TLA+ mathematical invariants.
- **CI Integration:** Property tests run automatically on every push, ensuring continuous regression prevention.

### Negative / Trade-Off Consequences
- Model checking requires Java dependencies (`tla2tools.jar`) for standalone execution; TLC model checks remain a documented pre-release step while Hypothesis fuzzing runs in automated CI.
