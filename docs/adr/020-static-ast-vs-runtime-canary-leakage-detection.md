# ADR-020: Dual-Layer Temporal Safety — Static AST Linting vs. Dynamic Runtime Canaries

* **Status:** Accepted
* **Date:** 2026-09-14
* **Authors:** Ledger Quantitative Data Engineering Team
* **Deciders:** Core Architecture Team
* **Technical Domain:** Safety Engineering, DevTools & Canary Architecture

---

## 1. Context & Problem Statement

Information leakage (lookahead bias, restatement leakage, un-lagged filing access, survivorship bias) is the most expensive failure mode in quantitative finance. It causes strategies that appear stellar in simulated backtests to collapse catastrophically in live capital deployment.

Traditionally, software engineering detects bugs via either:
1. **Static Analysis (Linters / Type Checkers):** Inspecting code before execution.
2. **Dynamic Testing (Unit & Integration Tests):** Running code against controlled test fixtures.

In quantitative feature engineering, neither approach is sufficient on its own:
- **Static Analysis Alone Is Blind to Data State:** A Python linter can inspect an AST, but it cannot know whether a financial filing at `2020-08-01` had an SEC acceptance timestamp of `16:05:00` or `17:30:00`, nor can it know if a historical universe excluded delisted bankrupt entities.
- **Runtime Testing Alone Is Reactive and Slow:** Runtime canary tests only run against specific pre-configured datasets. If a quant researcher writes a new custom alpha script containing `.shift(-1)` or a global `df['close'].mean()`, runtime backtest canaries won't catch it unless a dedicated test fixture was created for that exact script.

We need an integrated, multi-layered temporal safety architecture.

---

## 2. Decision

> **We adopt a Dual-Layer Defense-in-Depth Safety Architecture combining Static AST Linting (`ledger lint`) for pre-execution code analysis and Dynamic Runtime Canaries (`ledger canaries`) for operational invariant enforcement.**

```
+-------------------------------------------------------------------------+
| Layer 1: Pre-Execution Static AST Linter (ledger lint)                  |
| - Scans user strategy scripts BEFORE execution                          |
| - Catches syntactic lookahead antipatterns:                             |
|   * Negative index shifts: df['price'].shift(-k)                        |
|   * Full-sample normalization: df['col'].mean() / StandardScaler.fit()  |
|   * Unbounded forward-fills: .ffill() across bitemporal splits          |
|   * Non-PIT join keys on raw filing publication dates                   |
+-------------------------------------------------------------------------+
                                    |
                                    v (Executable Code Validated)
+-------------------------------------------------------------------------+
| Layer 2: Runtime Bitemporal Engine & Dynamic Canaries (ledger canaries) |
| - Validates data state, interval semantics, and market realities:       |
|   * Canary 01: Post-event restatements hidden before publication        |
|   * Canary 02: Dynamic CAF split invariance (pre-split vs post-split)   |
|   * Canary 03: After-hours publication lag to next session market open  |
|   * Canary 04: Survivorship-free universe snapshotting                  |
|   * Canary 05: SEC EDGAR filing lag windows                             |
|   * Canary 06: Deterministic sec_id entity relabeling                   |
+-------------------------------------------------------------------------+
```

### Complementary Coverage Matrix

| Failure Mode / Antipattern | Static AST Linter (`ledger lint`) | Dynamic Canary Suite (`ledger canaries`) |
| :--- | :---: | :---: |
| **Negative series shift (`.shift(-1)`)** | **Catches (Line & Col)** | Catches if integrated in backtest |
| **Full-sample z-score normalization** | **Catches (Line & Col)** | Missed if strategy still runs |
| **Un-windowed forward fill (`.ffill()`)** | **Catches (Line & Col)** | Missed without edge-case fixture |
| **SEC EDGAR 10-Q acceptance lag** | Blind (data dependent) | **Catches & Enforces Invariant** |
| **4-for-1 Split CAF Invariance** | Blind (data dependent) | **Catches & Enforces Invariant** |
| **Survivorship / Bankrupt entity exclusion** | Blind (data dependent) | **Catches & Enforces Invariant** |
| **Historical ticker symbol reuse** | Blind (data dependent) | **Catches & Enforces Invariant** |

---

## 3. Consequences

### Positive (Gains & Guarantees)
- **Immediate Feedback Loop:** Quants receive instant static linter warnings in their IDE or CLI in $< 50\text{ms}$ before initiating expensive backtest computations.
- **Rock-Solid Mathematical Invariants:** Canaries guarantee that the underlying storage, SQL views, and ASOF joins never violate bitemporal boundaries regardless of user strategy code.
- **Zero Blind Spots:** Complete end-to-end protection spanning both syntax (how code is written) and semantics (how data is resolved).

### Negative & Trade-offs (Liabilities & Mitigations)
- **Potential Static False Positives:** Static AST inspection cannot always infer runtime DataFrame types.
  - *Mitigation:* The linter explicitly targets pandas/polars method chains (`.shift()`, `.rolling()`, `.mean()`) and emits structured warnings with line snippets and actionable suggestions.

---

## 4. Alternatives Considered & Rejected

### Option A: Runtime Canary Suite Only
- **Why Rejected:** Leaves quant researchers vulnerable to writing subtle syntax bugs (e.g., negative shifts or full-sample standard deviations) that run silently and produce inflated backtests.

### Option B: Static Type-Checking / Mypy Only
- **Why Rejected:** Mypy verifies type signatures (`DataFrame -> DataFrame`), but cannot evaluate temporal semantics or lookahead logic.

---

## 5. References & Prior Art
- Python Standard Library: `ast` (Abstract Syntax Trees).
- Ledger Canary Catalog: `docs/canary_catalog.md`.
- Lopez de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley.
