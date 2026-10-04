# Review checklists

Use only the section matching the current review mode. The canonical rule documents contain the exact definitions and thresholds.

## New entry

- Confirm the security was in the eligible Japanese common-stock universe at the cutoff.
- Recalculate every hard gate from current filings.
- Recalculate the 100-point score; do not carry forward the previous total.
- Confirm score 70 or more, market-space score 8 or more, and reverse-feasibility score 10 or more.
- Record SAM, SOM, direct competitors, competitor enterprise values, and the 3-year reverse-tenbagger model.
- Fix up to three quarterly operating KPIs before purchase.
- Calculate fully diluted share count and financing runway.
- Check current price against the frozen limit-price rule.
- Check 20-day average turnover, five-day exit capacity, board lot, and every portfolio cap.
- Calculate formal month-end `MRS-v0.1`.
- Write the order and non-fill plan before returning `BUY`.

## Daily event

Search TDnet, EDINET, the company IR site, and JPX notices since the previous check. Evaluate only:

- audit opinion, going-concern issue, or insolvency;
- delayed, corrected, false, or investigated financial disclosure;
- variable-strike financing whose maximum dilution cannot be calculated;
- board-supported cash acquisition;
- trading suspension, delisting, merger, or share exchange;
- an updated score below 60 or impossible reverse-tenbagger scenario when enough new data exists.

Return `NO-ACTION` for ordinary price movement without an event rule.

## Weekly

- Reconcile all disclosures against the last-check timestamp.
- Update 20-day average turnover and position weight.
- List earnings and shareholder-event dates due in the next two weeks.
- Identify overdue monthly, quarterly, or annual reviews.
- Calculate a provisional market-regime snapshot and label it provisional.
- Do not change a formal month-end action solely from the provisional score.

## Monthly

- Freeze the information cutoff at the final trading close.
- Re-screen the point-in-time eligible universe.
- Update entry multiple, MA20, highest MA20, DD20, 3-year deadline, and position concentration.
- Apply the active rule version's price/time rules once in their documented priority, and record v0.3 separately when it is only a shadow challenger.
- Calculate formal `MRS-v0.1` from the same month-end date.
- Prepare next-business-day limit orders only after the pre-order disclosure check.

## Quarterly

- Read the earnings release, securities filing when available, presentation, and KPI appendix.
- Compare each frozen KPI with its base scenario and the same seasonal period.
- Recalculate per-share TTM gross profit.
- Recalculate score, fully diluted shares, financing runway, necessary revenue CAGR, and necessary market share.
- Check whether an exception labeled temporary ended by its logged date.
- Apply `S-B` first-occurrence and consecutive-quarter state correctly.
- Permit `ADD` only after every frozen add condition passes and before 5x.

## Source hierarchy

1. TDnet and EDINET filings
2. JPX notices and official market data
3. Company statutory filings, earnings releases, and IR data books
4. Cabinet Office and Bank of Japan statistics
5. Industry associations and direct-competitor primary material
6. Reporting and commercial databases for discovery only

For every source record the document title, issuer, publication timestamp, retrieval timestamp, URL, reporting period, units, and any restatement status.

## Failure states

Return `WAIT` instead of guessing when:

- a latest filing or material disclosure is unavailable;
- fully diluted shares cannot be bounded;
- a core KPI changed definition without a bridge;
- SAM, SOM, or the reverse calculation cannot be reproduced;
- liquidity or board-lot data is stale;
- one market-regime input is missing for a new or additional purchase;
- an exchange, broker, price, split, or calendar inconsistency is unresolved.
