# Week 2 Guide: Vectorized ASOF Engine & Dynamic Cumulative Adjustment Factors (CAF)

**Primary Objective:** Build the dynamic Cumulative Adjustment Factor (CAF) engine to adjust raw prices on-the-fly strictly as-of decision time, build the vectorized bitemporal `join_features_as_of` engine using Polars/DuckDB, and implement declarative technical feature views.

---

## 1. Definition of Done (DoD) for Week 2
- [ ] Mathematical Cumulative Adjustment Factor (CAF) engine implemented and unit-tested (`ledger/features/caf.py`).
- [ ] Vectorized `join_features_as_of()` engine implemented over arbitrary Observation Matrix inputs (`ledger/features/engine.py`).
- [ ] Declarative Feature Registry and Feature Definition base classes operational (`ledger/features/registry.py`).
- [ ] 3 core technical features implemented with dynamic CAF support: 20-day Momentum, 20-day Rolling Realized Volatility, and 50-day EMA (`ledger/features/definitions/technical.py`).
- [ ] Unit tests and performance benchmarks verifying zero look-ahead in CAF computation (`tests/unit/test_caf_math.py`, `tests/unit/test_asof_engine.py`).

---

## 2. Day-by-Day Implementation Plan

### Day 1: Dynamic Cumulative Adjustment Factor (CAF) Math
1. **Mathematical Formulation:**
   For any historical price date $t$ and observation decision timestamp $T_{obs}$:
   $$\text{CAF}(t, T_{obs}) = \prod_{k \in \mathcal{A}(t, T_{obs})} \text{split\_factor}_k$$
   where $\mathcal{A}(t, T_{obs}) = \{ k \mid t < \text{ex\_date}_k \le T_{obs} \land \text{known\_from}_k \le T_{obs} \}$.
2. **Implement `ledger/features/caf.py`:**
   - Write a vectorized Polars/DuckDB function that takes raw prices and corporate actions and calculates adjusted close on-the-fly:
     $$\text{adj\_close}(t, T_{obs}) = \text{raw\_close}(t) \times \text{CAF}(t, T_{obs})$$
3. **Unit Test in `tests/unit/test_caf_math.py`:**
   - Assert: For a stock that split 4:1 on 2020-08-31, querying as of 2020-08-15 yields $\text{CAF} = 1.0$ (unadjusted).
   - Assert: Querying as of 2020-09-01 yields $\text{CAF} = 4.0$ for historical prices before 2020-08-31.

### Day 2: Vectorized ASOF Join Engine
1. **Implement `ledger/features/engine.py`:**
   - Define the observation matrix schema: `entity_df` with columns `[sec_id, observation_timestamp]`.
   - Implement `join_features_as_of(entity_df: pl.DataFrame, feature_views: list[str]) -> pl.DataFrame`.
   - Leverage Polars `join_asof` or DuckDB native `ASOF JOIN`:
     ```python
     # Example Polars ASOF join pattern
     joined = entity_df.sort("observation_timestamp").join_asof(
         feature_df.sort("known_from"),
         left_on="observation_timestamp",
         right_on="known_from",
         by="sec_id",
         strategy="backward"
     )
     ```
   - Filter out any rows where `derived_known_to` is populated and `observation_timestamp >= derived_known_to`.

### Day 3: Declarative Feature Registry
1. **Implement `ledger/features/registry.py`:**
   - Define `FeatureDefinition` base class using Pydantic:
     - `name: str`
     - `version: str`
     - `dependencies: list[str]`
     - `compute_fn: Callable[[pl.DataFrame], pl.DataFrame]`
   - Build `FeatureRegistry` singleton to register, validate DAG dependencies, and resolve feature calculation order.

### Day 4: Technical Feature Views
1. **Implement `ledger/features/definitions/technical.py`:**
   - **`momentum_20d`:** $\frac{\text{adj\_close}(T_{obs})}{\text{adj\_close}(T_{obs} - 20\text{d})} - 1.0$ (dynamically adjusted via CAF as of $T_{obs}$).
   - **`volatility_20d`:** 20-day annualized rolling standard deviation of log returns.
   - **`sma_50d` / `ema_50d`:** Simple and Exponential Moving Averages on dynamically adjusted prices.
2. **Ensure Feature Invariants:**
   - Feature functions must never access prices where `trade_date > observation_timestamp` or corporate actions where `known_from > observation_timestamp`.

### Day 5: Verification & Benchmarking
1. **Write `tests/unit/test_asof_engine.py`:**
   - Test observation matrices with 1,000+ timestamps across multiple tickers.
   - Validate execution time: vectorized ASOF join should complete in $< 100\text{ms}$.
2. **Zero-Memory-Copy Check:**
   - Ensure Arrow table sharing between DuckDB and Polars without writing intermediary CSV/temp files.

---

## 3. Key Architectural Traps to Avoid in Week 2
- **Trap 1:** Applying a static split adjustment factor backwards across the whole time-series. If you compute a 20-day return across a split date using static factors, you leak future split knowledge.
- **Trap 2:** Row-by-row iteration in Python (`for row in df.iter_rows()`). Always use vectorized Polars/DuckDB ASOF joins.
- **Trap 3:** Confusing `observation_timestamp` with `trade_date`. The observation timestamp is when the decision is made (e.g. 2023-01-05 16:15:00), which joins to the most recent historical trade date available as of that exact moment.
