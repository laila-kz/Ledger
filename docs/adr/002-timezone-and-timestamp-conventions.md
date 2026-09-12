# ADR-002: Timezone and Timestamp Actionability Conventions

* **Status:** Accepted
* **Date:** 2026-09-12
* **Authors:** Ledger Quantitative Data Engineering Team
* **Deciders:** Core Architecture Team
* **Technical Domain:** Core Bitemporal Engine & Calendar Subsystem

---

## 1. Context & Problem Statement

Quantitative backtests suffer severe look-ahead leakage when timestamps are treated as abstract continuous numbers rather than realistic, actionable events subject to real-world market operating hours and data dissemination delays:

1. **Timezone Ambiguity & DST Jumps:** Raw financial data sources mix timestamps across timezones (e.g., SEC EDGAR filings timestamped in UTC or US Eastern, EOD daily bars labeled by calendar date, macroeconomic releases in Washington D.C. local time). Naive timestamps lead to 4-to-5 hour leakage or lag during Daylight Saving Time (EST vs. EDT) transitions.
2. **Instantaneous Ingestion Illusion:** An earnings report filed on Friday at 17:30 EST cannot be traded on Friday at 15:59 EST before the market close. A model that consumes the Friday filing during the Friday close rebalance introduces massive look-ahead bias.
3. **End-of-Day Bar Consolidation Delay:** Daily OHLCV bars do not become actionable the millisecond the 16:00:00 closing cross occurs. Auction closing, tape consolidation, and vendor feeds require post-market processing before features are available.

We require an explicit, system-wide convention for timezone normalization, interval boundary semantics, and actionable knowledge time resolution.

---

## 2. Decision

> **We standardize on UTC for persistent storage, `America/New_York` for exchange session boundary calculations, half-open interval semantics $[start, end)$, and an explicit `get_actionable_timestamp()` resolution rule for all ingested data.**

### Key Architectural Conventions

1. **Storage & Memory Timezone Representation:**
   * All timestamps stored in Parquet and DuckDB tables must be timezone-aware or explicitly normalized to UTC.
   * Session boundary logic in `ledger/core/calendars.py` maps timestamps to `America/New_York` (XNYS / NYSE calendar) to accurately handle 09:30 market opens, 16:00 standard closes, 13:00 early closes, and US holidays.

2. **Half-Open Interval Semantics:**
   * All bitemporal intervals operate as half-open ranges: $[valid\_from, valid\_to)$ and $[known\_from, known\_to)$.
   * A point $T$ is included in interval $I$ if and only if $start \le T < end$ (where $end = +\infty$ when `NULL`).
   * This guarantees that when a record is superseded at $T_{new}$, the prior version ceases to be visible precisely at $T_{new}$ without overlap or gap.

3. **Actionable Timestamp Rules (`get_actionable_timestamp`):**
   * **EOD Market Data (`is_market_data=True`):**
     * Daily price bars for session date $D$ close at 16:00 EST (or 13:00 on early-close sessions like Black Friday).
     * Actionable timestamp is computed as `session_close + 15 minutes` (16:15 EST / 13:15 EST).
   * **Fundamental Filings & Reports (`is_market_data=False`):**
     * **In-Session (09:30 $\le T_{event} < 16:00$ EST on a trading day):** Actionable immediately at $T_{event}$.
     * **After-Hours ($T_{event} \ge 16:00$ EST):** Actionable at the next trading session open (**09:30 EST**).
     * **Pre-Market ($T_{event} < 09:30$ EST):** Actionable at today's market open (**09:30 EST**).
     * **Weekend / Exchange Holiday:** Actionable at the next trading session open (**09:30 EST**).

---

## 3. Consequences

### Positive (Gains & Guarantees)
* **Elimination of After-Hours Look-Ahead (Canary 03 Protection):** Friday evening earnings releases can never be accidentally consumed by a Friday 15:59 rebalance strategy.
* **Deterministic DST Handling:** Daylight Saving Time shifts (EST $\leftrightarrow$ EDT) are handled through `exchange_calendars` and `zoneinfo.ZoneInfo("America/New_York")`, avoiding 1-hour time warp defects.
* **Mathematical Interval Partitioning:** Half-open intervals ensure non-overlapping partitions during vectorized joins.

### Negative & Trade-offs (Liabilities & Mitigations)
* **Calendar Lookup Overhead:** Determining `next_session()` and `is_session()` requires exchange calendar indexing.
  * *Mitigation:* `NYSECalendarService` uses cached calendar tables loaded once per runtime session.
* **Intraday Bar Resolution Boundary:** High-frequency 1-second order book consolidation is intentionally outside the daily/filing scope of this system.

---

## 4. Alternatives Considered & Rejected

### Option A: Scalar Timestamps without Exchange Calendar Awareness
* **Description:** Treat all events as actionable at `event_time` in UTC without checking whether NYSE is open.
* **Why Rejected:** Directly causes look-ahead leakage during backtesting (e.g. trading Friday after-hours earnings as if they were known before the Friday closing bell).

### Option B: Closed Intervals $[start, end]$
* **Description:** Both lower and upper bounds inclusive.
* **Why Rejected:** Creates duplicate boundary collisions at timestamp $T$ when a restatement is published at $T$, requiring complex tiebreaking in SQL joins.

### Option C: Fixed Global 24-Hour Delay Rule
* **Description:** Add a static 24-hour buffer to all filings.
* **Why Rejected:** Distorts reality; Friday 17:00 filings would become actionable Saturday 17:00 (when markets are closed), and Monday 10:00 filings would be delayed until Tuesday despite being tradable on Monday afternoon.

---

## 5. References & Prior Art
* Richard T. Snodgrass, *Developing Time-Oriented Database Applications in SQL* (1999).
* Python `exchange_calendars` library specification (XNYS).
* Ledger Canary Suite: `tests/canaries/test_canary_03_after_hours_session.py`.
