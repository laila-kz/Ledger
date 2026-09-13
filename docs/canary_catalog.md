# Leakage Canary Catalog

Ledger's canaries pair a point-in-time result with a deliberately leaky reference
pipeline. A canary passes only when the PIT result matches the known truth and the
naive result diverges.

## Canary 01: Restated Fundamentals

- **Defect:** The latest restated filing is exposed to observations before the amendment was known.
- **Naive behavior:** Selects the latest filing version for every observation.
- **PIT assertion:** The original EPS is `$1.00` before `2023-11-15 17:30 UTC`; the amended EPS is `$0.70` at and after that timestamp.

## Canary 02: Retroactive Split Adjustment

- **Defect:** A future split is applied to historical prices before the split is known.
- **Naive behavior:** Applies every split factor statically to the complete price history.
- **PIT assertion:** The July price has `CAF=1.0` before the split is known and `CAF=4.0` afterward.
- **Convention:** Ledger multiplies historical prices by the split ratio, so `$400` becomes `$1600` after the split is known.

## Canary 03: After-Hours Session

- **Defect:** A filing released after the market close is consumed during the closed session.
- **Naive behavior:** Uses the raw event timestamp as the actionable timestamp.
- **PIT assertion:** A Friday `17:00 America/New_York` filing becomes actionable Monday at `09:30 America/New_York` (`13:30 UTC` in April 2023).

## Canary 04: Survivorship Universe

- **Defect:** Historical entities are removed because the universe is built from current constituents.
- **Naive behavior:** Uses a static modern constituent list for all historical dates.
- **PIT assertion:** A delisted entity is present before its delisting and absent afterward.

## Canary 05: Filing Lag Window

- **Defect:** A fiscal period-end date is treated as the date its data became known.
- **Naive behavior:** Joins fundamentals on `fiscal_period_end` instead of `known_from`.
- **PIT assertion:** The prior quarter remains visible until the Q1 10-Q acceptance timestamp; Q1 becomes visible only at `2023-05-10 16:30 UTC`.

## Canary 06: Ticker Re-identification

- **Defect:** Ticker changes split or duplicate an entity's historical record.
- **Naive behavior:** Treats ticker as the permanent security identifier.
- **PIT assertion:** The permanent `sec_id` remains continuous while the active alias changes from `FB` to `META`.
