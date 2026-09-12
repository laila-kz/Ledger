# ADR-007: Declarative Feature Registry, DAG Dependency Resolution, and Lineage Hashing

* **Status:** Accepted
* **Date:** 2026-09-12
* **Authors:** Ledger Quantitative Data Engineering Team
* **Deciders:** Core Architecture Team
* **Technical Domain:** Feature Store / Declarative Registry / DAG Engine / Content-Addressable Lineage

---

## 1. Context & Problem Statement

In quantitative backtesting and feature engineering, feature calculation logic rapidly expands from basic price adjustments (CAF) to dozens of technical indicators (Momentum, Realized Volatility, EMA, RSI) and fundamental ratios (P/E, EV/EBITDA).

Without a structured registry and dependency management system, data pipelines suffer from four critical flaws:

1. **Ad-Hoc Computation Ordering:** Features that depend on other computed features (e.g. `momentum_20d` requiring dynamically split-adjusted `adj_close`) require manual ordering scripts. Reordering errors or omitted intermediate transformations result in runtime `KeyError` exceptions or silent calculation corruption.
2. **Circular Dependencies:** Mutual or indirect dependency chains ($A \to B \to C \to A$) can cause infinite recursion or deadlocks if not detected before pipeline execution.
3. **Implicit & Bloated Function Signatures:** As new context data (corporate actions, fundamental filings, calendar session bounds) is introduced, modifying every feature function signature breaks backward compatibility.
4. **Lack of Provenance & Lineage:** In financial backtesting, demonstrating that a specific backtest result was generated with an exact version of a feature formula is essential to guard against stealth formula drift and look-ahead fixes.

We need a declarative registry that manages feature metadata, validates dependency DAGs, determines deterministic execution orders, and produces deterministic cryptographic hashes for Week 4 lineage manifests.

---

## 2. Decision

> **We will implement a declarative feature registry (`ledger.features.registry.FeatureRegistry`) using Pydantic metadata definitions (`FeatureDefinition`), Kahn's algorithm for topological sorting and cycle detection, the `FeatureContext` object pattern for uniform function signatures, and deterministic SHA-256 source-code hashing for reproducible lineage manifests.**

### Key Architectural Components

1. **`FeatureDefinition` (Declarative Specification):**
   - Each feature is declared with `name`, `version`, `dependencies: list[str]`, `compute_fn: Callable[[FeatureContext], pl.DataFrame]`, `description`, and `tags`.
   - Feature definitions are immutable Pydantic models with strict validation.

2. **`FeatureContext` (Uniform Parameter Bundling):**
   - Feature compute functions accept a single `FeatureContext` containing `observations`, `prices`, `splits`, `catalog`, and `custom` dictionaries.
   - Future data sources (e.g. SEC EDGAR filings) can be added to `FeatureContext` without altering existing feature function signatures.

3. **`FeatureRegistry` (DAG Engine & Kahn's Algorithm):**
   - Builds the execution subgraph for any subset of requested target features.
   - Executes Kahn's algorithm (in-degree tracking) to resolve the topological execution order.
   - Rejects cycles with `CyclicDependencyError` and missing upstream dependencies with `FeatureNotFoundError`.

4. **`@register` Decorator:**
   - Provides idiomatic, zero-boilerplate registration at module import time while supporting isolated custom `FeatureRegistry` instances for testing.

5. **Deterministic Lineage Hashing (`compute_hash`):**
   - Hashes feature `name`, `version`, `dependencies`, and normalized AST/source code of `compute_fn` into a SHA-256 string (e.g. `sha256:7f83b16...`).
   - Changes to formulation, constants, or version string alter the hash, enabling Week 4 lineage manifests to guarantee auditability.

---

## 3. Rationale & Alternatives Considered

### 3.1. Why Kahn's Algorithm for Topological Sort?
* **Alternative:** Depth-First Search (DFS) with recursive cycle detection.
* **Decision Rationale:** Kahn's algorithm operates iteratively on node in-degrees ($O(V + E)$). It naturally isolates unreachable features, produces deterministic ordering via sorted zero-in-degree queues, and provides clear error diagnostics listing all unresolved cycle nodes when in-degrees remain positive.

### 3.2. Why Source-Code & AST Hashing?
* **Alternative:** Hashing only the semantic version string (e.g. `"1.0.0"`).
* **Decision Rationale:** Developers often tweak feature formulas (e.g. changing an EMA alpha or volatility annualization factor from 252 to 365) without bumping the version string. By hashing the normalized source code together with the version and dependencies, any silent code modification invalidates the manifest hash.

### 3.3. Why the `FeatureContext` Object Pattern?
* **Alternative:** Variable `*args` / `**kwargs` or explicit positional parameters.
* **Decision Rationale:** Explicit parameters require modifying all feature callables when adding a new store input. `**kwargs` lacks type safety and IDE autocompletion. `FeatureContext` provides type safety, autocompletion, and extensibility.

### 3.4. Why Instance-Based + Global Singleton?
* **Decision Rationale:** Module-level convenience requires a global `@register` decorator so that importing `ledger.features.definitions.technical` auto-registers all standard features. However, unit tests require clean, isolated `FeatureRegistry()` instances to test error conditions and synthetic DAGs without polluting global state. Supporting both satisfies production ergonomics and test isolation.

---

## 4. Consequences

### Positive
- **Declarative Modularity:** Adding new features requires only writing a function decorated with `@register`.
- **Zero Calculation Ordering Bugs:** Topological resolution guarantees all required dependencies are computed before dependents.
- **Fail-Fast Cycle Detection:** Circular references fail at registration/resolution time rather than during data execution.
- **Lineage Ready:** Feature hashes integrate seamlessly into Week 4's `manifest.json`.

### Trade-Offs & Mitigations
- **Dynamically Generated Functions:** Dynamic lambdas or compiled C-extensions cannot inspect Python source text. *Mitigation:* `compute_hash()` falls back to `__qualname__` and version metadata if `inspect.getsource()` is unavailable.
- **External View Dependencies:** Feature dependencies may refer to raw database views (e.g. `fact_market_ohlcv_raw`) rather than registered features. *Mitigation:* In Day 4/5, raw views can be registered as base leaf nodes with empty dependency lists.
