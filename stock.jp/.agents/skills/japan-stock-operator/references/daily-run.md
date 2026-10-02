# Daily Run

Use this workflow when the request is “today's run,” a scheduled operation, or a resume of an incomplete run.

## Start or resume once at night

1. Read `docs/operations/nightly-operation-v0.1.md`, `docs/operations/daily-automation-runbook-v0.1.md`, `operations/private/operation-policy.json`, and the canonical rule files named in `SKILL.md`.
2. The assistant owns collection, research, analysis, and PAPER processing at the planned evening 21:00 Asia/Tokyo check. The full run remains on weekdays; on weekends review only due or unfinished items, without inventing a trading session or paper fill, and keep the next full run at the next weekday 21:00. First validate the explicitly configured private-local market archive using `docs/operations/daily-stock-price-actions-v0.1.md`; Actions only validate stored data and must never be used to collect or mutate. Scheduler registration and repository publication require their separate setup/authorization. Then run `scripts/operation_bootstrap.py check`. It must verify the private archive checksum, at least 98% full-market coverage, 100% active-target coverage, and freshness. Stop on any PAPER blocker and verify the configured provider identity; a price feed does not replace other primary-source checks.
3. Run `scripts/nightly_operation.py start --at <current aware JST timestamp> --cutoff <declared JST cutoff>` and retain the returned `run_token`. Start repeats readiness before creating a run, scans EDINET, and creates due first-party checks. Confirm company IR, TDnet, official prices/corporate actions, and the next JPX cash-equity trading date from their primary pages before closing the run. If it returns `locked`, do not start a second run.
4. Read `provider-health.json`, `research-queue.json`, `work-plan.json`, `coverage.json`, `operations/private/state.json`, the previous run's `handoff.json` when present, `portfolio-register.csv`, `watchlist.csv`, and the trade, recovered-capital, cash, corporate-action, rebuy-restriction, and industry-exposure ledgers.
5. If the returned run status is already `completed`, report that the date was already closed. Do not create duplicate orders.
6. If it is `in_progress`, resume the existing files rather than replacing them.

## Cover the required universe

Process every task in both `research-queue.json` and `work-plan.json`. Check TDnet, company IR, official prices/corporate actions, JPX notices, and the cash-equity calendar in their primary sites. Mark a task `COMPLETED` only with `evidence_source_ids`; defer it only by copying the same task ID into `handoff.pending_reviews`.

Check all current holdings and pending orders. Check active watchlist names for new official disclosures, but do not fully rescore every watchlist name every day. Use the prior successful disclosure cutoff as the lower bound and the declared current cutoff as the upper bound.

If holdings and active watchlist are both empty, do not block PAPER and do not invent a stock action. Keep the generated `GLOBAL / NO-ACTION`, complete the `initial_universe_review` from the accumulated full-market archive, narrow candidates, verify primary company evidence for those candidates, and only then activate a small watchlist for later daily runs.

Include other modes only when due:

- Daily event checks on every run
- Weekly checks on Friday or when a missed weekly review is queued
- Monthly checks after the final trading session's values are available or when queued
- Quarterly checks when a new quarterly disclosure starts the five-trading-day review window
- Full-year checks when a new full-year disclosure starts the ten-trading-day review window

For a monthly check, use the already accumulated daily full-market archive and the current JPX list to revise the candidate set. Do not repeat the same Yahoo acquisition; review primary evidence only for the narrowed candidates.

Record due work that cannot be completed in `handoff.json` under `pending_reviews`. Do not silently drop it.

## Write durable artifacts

Update the current run's:

- `work-plan.json` with every due review in `COMPLETED` or explicit `DEFERRED`
- `research-results.md` with a completed, human-readable summary of all due research
- `global-risk.md` with completed facts, transmission to portfolio KPIs, and the resulting judgment for FX, rates, resources, major-country policy, and geopolitics
- `next-day-actions.csv` with exactly one auditable next-session action for every holding and watchlist code
- `report.md` with coverage, exceptions, decisions, due reviews, data gaps, human actions, and the next run
- `sources.csv` with publication time, retrieval time, URL, and whether it is a primary source
- `coverage.json` by moving every actually checked expected item to `checked` and marking evidenced official sources `CHECKED`
- `orders.csv` only through `scripts/order_ticket.py propose` after every research task is `COMPLETED`; if any research task is `DEFERRED`, change the affected trade action to `WAIT`. Use the policy-derived status (`PAPER_PROPOSED` in `PAPER`, `PROPOSED` only in an approved `LIVE` mode)
- `pretrade-check.md` with the target trade date when an order is proposed
- individual decision logs only for an actionable decision or a due periodic review
- `handoff.json` with `pending_reviews`, `pending_orders`, `data_gaps`, and `next_run_at_jst`

Reconcile fill or cancellation data already entered by the user before preparing another order. Never infer an execution from a proposed order.

Append each proposal, paper fill, human-reported fill, cancellation, and expiration to `trade-event-ledger.csv`. Update position, capital, recovered-capital, rebuy-restriction, and industry ledgers only from a recorded event. Do not create another ticket for a code while its prior ticket remains unreconciled.

## Close safely

Call `scripts/nightly_operation.py finalize` with the same `run_token` only after all holdings, pending orders, required disclosure sources, due tasks, research results, global risk, and next-day actions have been checked through the declared cutoff. A non-critical data gap may remain only as a structured item with its impact and retry time.

Call `fail` with the same `run_token` if a required source or a material part of the universe could not be checked. A failed run must not advance the successful disclosure cutoff. Leave partial files intact for audit and retry.

Never submit a brokerage order. Require the user to complete the 8:45–8:55 pre-trade checklist and manually approve each `LIVE` proposed order. After a successful finalize, stop active work and wait for the next scheduled night; `PAPER` requires no morning action.

## Return

Lead with the run status: `COMPLETED`, `FAILED`, or `ALREADY-COMPLETED`. Then give:

1. cutoff and coverage;
2. urgent decisions;
3. next-session proposed orders;
4. human actions;
5. data gaps and queued reviews;
6. saved research, next-day actions, orders, report, and handoff paths;
7. the next scheduled nightly run time.

Market data acquisition is paused pending provider permission review. Do not run the bulk Yahoo collector; use the subscribed J-Quants provider only after secure credential setup and provider validation. Missing private data means WAIT, with no public archive fallback. Independent official IR/disclosure research may continue in separate private notes but must not be recorded as a completed nightly/PAPER run. See the storage procedure above.

## Publish daily results through PRs

The owner authorized non-draft daily market-data/result PRs with native auto-merge,
not direct main writes. Follow `docs/daily-pr-publication.md`: validate allowlisted
paths and semantic integrity, wait for every expected exact-head CI check, re-read
the PR/main state, then enable native auto-merge using the authorized connector.
Both daily kinds target stock.jp.private; market paths start with stock.jp/data/jquants-archive/.
Run daily_publication.py with --root pointing to the Git root; require format, test and guardrail.
Code, dependency, policy, risk, configuration and security PRs keep normal review.
Do not use Actions or extract AUTOMATION_PAT for PR writes. Report partial market
acquisition honestly; it is not a completed nightly run. A failed run may have a
preservation PR, but cannot claim completed-result auto-merge eligibility.
