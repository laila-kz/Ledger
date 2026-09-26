# Ledger Formal Verification (TLA+ & TLC Model Checker)

This directory contains the formal mathematical specification and model checking configuration for the **Ledger Bitemporal Interval Derivation Engine**.

---

## What is Formally Specified?

While example-based tests (`pytest`, Canary Suite) verify specific execution scenarios, **TLA+ model checking** exhaustively explores all reachable state transitions within bounded domains to mathematically prove the absence of invariant violations.

The state machine in [`Ledger.tla`](Ledger.tla) models append-only ingestion and windowed `LEAD()` interval derivations (`known_to`, `valid_to`), verifying five core safety invariants:

1. **`NoOverlap`**: For any given entity (`sec_id`), no two transaction intervals $[known\_from, known\_to)$ overlap in time.
2. **`Monotonic`**: Transaction start time strictly precedes derived $known\_to$ ($known\_from < known\_to$, or $known\_to = \text{NULL}$).
3. **`NoGaps`**: Adjacent derived versions leave zero temporal gaps between $known\_to_{n}$ and $known\_from_{n+1}$.
4. **`ValidBeforeKnown`**: Temporal causality holds ($known\_from \ge valid\_from$); a fact cannot be known prior to its real-world occurrence.
5. **`IdempotentReplay`**: Duplicate event re-ingestion breaks ties deterministically via monotonic sequence keys (`ingestion_seq`) without corrupting interval bounds.

---

## File Overview

- **[`Ledger.tla`](Ledger.tla)**: TLA+ specification file defining state variables, transition actions, derivation operators, and safety invariants.
- **[`Ledger.cfg`](Ledger.cfg)**: TLC model checker configuration file defining constant bounds ($SecIDs$, $MaxTime$, $MaxEvents$) and invariant assertions.
- **[`tlc_run_log.txt`](tlc_run_log.txt)**: Captured TLC model checker execution log showing 22,158 distinct reachable states explored with zero errors.

---

## How to Re-Run TLC Model Checking

### Option 1: VS Code TLA+ Extension (Recommended)
1. Install the **TLA+ Extension** for VS Code (`alyousef.vscode-tlaplus`).
2. Open [`docs/formal/Ledger.tla`](Ledger.tla).
3. Right-click in the editor and select **Check Model with TLC**.

### Option 2: Headless Command Line (tla2tools.jar)
1. Download `tla2tools.jar` from the official [TLA+ Releases](https://github.com/tlaplus/tlaplus/releases).
2. Run TLC from the repository root:
   ```bash
   java -cp tla2tools.jar tlc2.TLC -config docs/formal/Ledger.cfg docs/formal/Ledger.tla
   ```
