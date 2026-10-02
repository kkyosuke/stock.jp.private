---
name: japan-stock-operator
description: Run and resume one-shot nightly Japanese-stock operations or review individual stocks under this repository's frozen tenbagger rules. Research all due items, produce auditable BUY, WATCH, WAIT, KEEP, ADD, REDUCE, SELL, or NO-ACTION decisions for the next session, create policy-compliant proposed order tickets, save durable handoff state, and wait until the next night. Use for scheduled nightly runs, live purchase timing, pre-order checks, disclosure monitoring, weekly or monthly reviews, quarterly progress reviews, market-regime sizing, exit checks, and operation logs.
---

# Japan Stock Operator

Apply the repository's frozen rules to current public information. Produce a reviewable decision record; never submit a brokerage order.

## Load the canonical rules

From the vendored `stock.jp/` code directory (not the Git root), read these files before judging a stock:

1. `docs/operations/live-operation-playbook-v0.1.md`
2. `docs/operations/operation-governance-v0.1.md`
3. `docs/rules/tenbagger-rule-v0.2.md`
4. `docs/rules/tenbagger-rule-v0.3.md` for shadow comparison or after explicit promotion
5. `docs/rules/tenbagger-rule-v0.4.md` for the active full-tenbagger PAPER allocation

Read [references/review-checklists.md](references/review-checklists.md) for the checklist matching the requested review mode. Treat the canonical documents as authoritative if this skill conflicts with them.

For a scheduled run, “today's run,” “today's nightly operation,” or a request to execute all due operations, also read [references/daily-run.md](references/daily-run.md), `docs/operations/nightly-operation-v0.1.md`, `docs/operations/operation-readiness-v0.1.md`, and `docs/operations/daily-automation-runbook-v0.1.md`.

## Choose a review mode

- `daily-run`: resume durable state, execute the nightly official-source check and all due periodic modes, set every target's next-day action, save proposed tickets and handoff, then wait until the next night
- `new-entry`: full gate, score, reverse-tenbagger, liquidity, regime, and order-plan review
- `daily-event`: check new official disclosures and immediate `S-A` triggers only
- `weekly`: check missed disclosures, liquidity, concentration, upcoming reviews, and a provisional regime snapshot; normally return `NO-ACTION`
- `monthly`: run candidate selection, the active rule version's price/time rules, shadow comparison, concentration, formal regime score, and next-day order planning
- `quarterly`: fully rescore company progress and decide `KEEP`, `ADD`, `REDUCE`, or `SELL`
- `ad-hoc`: evaluate a specified public event without inventing a general review

If the user asks for “today's run” or all necessary operations, choose `daily-run`. Otherwise infer the narrowest applicable mode and state it.

## Establish the information cutoff

1. Record the evaluation time in JST.
2. Search current official sources. Prefer TDnet, EDINET, JPX, the company's IR site, Cabinet Office, and Bank of Japan.
3. Record each source's publication time, retrieval time, and URL.
4. Use only information public by the declared cutoff.
5. Label press reports and social posts as discovery sources; confirm their claims in primary sources.
6. Return `WAIT` when a hard-gate input, current filing, share-count input, or required market-regime input is missing.

Never infer an unpublished KPI, silently carry forward a stale score, or treat search snippets as evidence.

## Separate fact, calculation, and judgment

- `FACT`: reproduce the disclosed meaning with units, period, and source.
- `CALCULATION`: show the formula, denominator, dilution basis, currency, and rounding.
- `JUDGMENT`: cite the exact rule ID and explain why the facts and calculations satisfy it.

Do not use narrative confidence as a substitute for a missing rule input.

## Apply decisions in order

Read `operations/private/operation-policy.json` before creating an order ticket. In `PAPER`, use the policy's active rule version; the current baseline is v0.4, with v0.2 and v0.3 as labeled shadow counterfactuals. Keep every PAPER ticket `PAPER_PROPOSED`. Never treat a shadow version as live merely because its rules are simpler. In `PAUSED`, create no order ticket. In `LIVE`, still keep brokerage submission human-only.

1. Check all immediate `S-A` exit rules.
2. Check quarterly `S-B` reduction and exit rules when applicable.
3. Apply the active rule version's price, time, and partial-profit rules in its documented priority.
4. Record each shadow version's different action as a counterfactual; never execute it.
5. Check concentration reduction.
6. For new or additional purchases, calculate `MRS-v0.1` and apply its entry multiplier.
7. Apply liquidity, lot-size, position, industry, candidate-pool, available-cash, and maximum-holdings caps. Under v0.4, classify every uninvested amount as strategy waiting cash, never as a fixed safety allocation.

Market regime may reduce or pause a new or additional purchase. It must not trigger a sale by itself or override a company-specific exit.

## Calculate the market regime

Use point-in-time month-end inputs and run:

```bash
.venv/bin/python scripts/market_regime.py <frozen inputs>
```

Use `NORMAL`, `CAUTION`, or `STRESS` exactly as returned. If any required input is unavailable, record `UNAVAILABLE` and do not create a new or additional position. Preserve the unscaled counterfactual amount in the log.

## Create the decision log

Generate a private draft when a concrete company is under review:

```bash
.venv/bin/python scripts/new_operation_log.py \
  --date YYYY-MM-DD --code CODE --company NAME --mode MODE
```

Fill every applicable field. Store exact quantities, wealth, tax, and account details only under `operations/private/`, which is linked to the single private repository root operations/private directory. Never expose account numbers, credentials, personal identifiers, or total personal assets in chat or public tracked files. Never store credentials in the repository.

In `daily-run` mode, use `scripts/nightly_operation.py start` as the only entry point. Complete `work-plan.json`, `research-results.md`, and every target row in `next-day-actions.csv`. Use `scripts/order_ticket.py propose` for every trade action; never edit an unmatched order into existence. Finalize with the same run token, report the next-day actions, then perform no further checks until the next scheduled night. Do not create one decision log per unchanged holding. Create detailed logs only for an actionable decision or a due periodic review, and summarize all other coverage in the daily report. Never mark a proposed order as submitted or filled without user-provided execution evidence.

## Return a fixed summary

Lead with one action: `BUY`, `WATCH`, `WAIT`, `KEEP`, `ADD`, `REDUCE`, `SELL`, or `NO-ACTION`.

Then report:

1. Evaluation time and mode
2. Rule IDs that determined the action
3. Position-size ceiling as a percentage, not a personal currency amount
4. `MRS-v0.1` score, state, and component results
5. Three strongest supporting facts
6. Three strongest risks or missing inputs
7. Exact order plan or reason no order is allowed
8. Next review trigger and date
9. Primary-source links
10. Private decision-log path, if created

Do not write “buy” or “sell” without rule IDs and evidence. Do not imply certainty or guaranteed returns. Require human confirmation before any brokerage order entry. `PAPER_PROPOSED` is virtual; even an approved `LIVE` run produces only `PROPOSED` tickets for the user's next-morning pre-trade check.
