# ADR 007: Exponential Moving Average (EMA) Seeding & Formulation Convention

## Context
In Quantitative Finance and Technical Analysis, Exponential Moving Averages (EMA) with span $N$ are recursive filters governed by smoothing factor:
$$\alpha = \frac{2}{N + 1}$$
$$EMA_t = \alpha \cdot P_t + (1 - \alpha) \cdot EMA_{t-1}$$

Two primary numerical conventions exist across industry tooling:
1. **Polars / Pandas `adjust=False` Convention**:
   - Initialises recursion from the very first observation ($EMA_0 = P_0$).
   - Computes recursively for all $t \ge 1$ and nulls out the leading $N-1$ bars when configured with `min_samples=N`.
2. **TA-Lib / Bloomberg Convention**:
   - Initialises the seed at bar $N-1$ using the $N$-period Simple Moving Average ($EMA_{N-1} = SMA_N$).
   - Evaluates recursive weighting strictly for $t \ge N$.

## Decision
For high-throughput vectorised operations in Polars, `ledger` implements `pl.col("adj_close").ewm_mean(span=50, min_samples=50, adjust=False)`.

Key characteristics:
- **Warm-up guarantees**: Exactly $N-1 = 49$ leading nulls for 50-day window, emitting the first valid non-null value at bar index 49 (the 50th observation).
- **Decay properties**: The memory difference between $P_0$ seeding and $SMA_{50}$ seeding decays exponentially by $(1 - \alpha)^k = (49/51)^k \approx (0.96078)^k$. For $k \ge 50$, the relative divergence is under $0.4\%$ and decays asymptotically to zero over longer histories.
- **Vectorized Performance**: Executes purely inside the compiled Rust engine without Python-level iteration or custom C-bindings.

## Consequences
- No external C/TA-Lib dependencies required in the core engine runtime.
- Exact reproducibility with Polars and standard quant data pipelines.
- Quant consumers requiring strict bitwise TA-Lib SMA-seeding parity are aware of the initial warmup decay profile documented here.
