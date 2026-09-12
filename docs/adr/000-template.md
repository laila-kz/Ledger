# [ADR-000] Title: [Short Sentence Describing the Decision]

* **Status:** [Proposed | Accepted | Superseded | Deprecated]
* **Date:** YYYY-MM-DD
* **Authors:** [Author Name / Team]
* **Deciders:** [List of Decision Makers]
* **Technical Domain:** [Storage | Ingestion | ASOF Engine | Lineage | Canaries]

---

## 1. Context & Problem Statement

Describe the technical context, requirements, and forces at play. 
- What problem are we solving?
- What constraints (performance, correctness, bitemporal safety, developer ergonomics) must we honor?
- Why is an architectural decision needed now?

---

## 2. Decision

State the decision clearly and concisely in active voice:
> **We will [action / pattern / architecture] to [solve problem], by [mechanism].**

Key architectural details and design guarantees:
1. **[Detail 1]:** Explanation of how this works.
2. **[Detail 2]:** Invariants maintained.
3. **[Detail 3]:** Boundaries and interface.

---

## 3. Consequences

### Positive (Gains & Guarantees)
* **[Gain 1]:** Measurable benefit or invariant achieved.
* **[Gain 2]:** Simplification or performance boost.

### Negative & Trade-offs (Liabilities & Mitigations)
* **[Trade-off 1]:** Increased query complexity or latency, mitigated by `[Mitigation]`.
* **[Trade-off 2]:** Operational requirement or constraint.

---

## 4. Alternatives Considered & Rejected

### Option A: [Alternative Name]
* **Description:** How it works.
* **Why Rejected:** Specific reason it fails our constraints (e.g., introduces look-ahead risk, mutates storage files, requires heavy infra).

### Option B: [Alternative Name]
* **Description:** How it works.
* **Why Rejected:** Technical rationale for rejection.

---

## 5. References & Prior Art
* [Link/Citation 1]
* [Link/Citation 2]
