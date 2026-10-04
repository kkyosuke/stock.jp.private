# Daily Run

Run commands from the repository root. Read `docs/operations/runbook.md`,
`docs/operations/market-data.md`, `docs/operations/automation.md`,
`config/policy.json`, `config/sources.json`, and the active rules before starting.
The configuration defines current thresholds, providers, timing, and permissions.

## Start or resume

1. Restore and verify adopted data with `python -m stock_jp data restore`.
2. Run `python -m stock_jp operation readiness check --at <aware JST timestamp>`.
   Stop on a PAPER blocker. Required input cannot be replaced with an old or unofficial feed.
3. Run `python -m stock_jp operation run start --at <JST> --cutoff <JST>`.
   Keep its `run_token`. Resume existing unfinished work; respect a returned lock.
4. Read the run's work plan, source queue, coverage, provider health, prior handoff,
   `operations/state.json`, `operations/inputs/watchlist.csv`, and `operations/ledgers/`.
5. If the run is already completed, report that result without adding orders or history.

## Complete the work

Check every holding, pending order, active watchlist name, and due review through the
frozen disclosure cutoff. Prefer official IR, TDnet, EDINET, JPX, and exchange calendars.
Record publication time, retrieval time, source URL, rule ID, and the resulting action.
Do not infer a fill, source observation, score, approval, or trading session.

For an empty portfolio and watchlist, complete the initial universe review and record
`GLOBAL / NO-ACTION`. Do not invent a stock action. Add candidates only after research.

Update the run's `work-plan.json`, `research-results.md`, `global-risk.md`,
`next-day-actions.csv`, `sources.csv`, `coverage.json`, `report.md`, and `handoff.json`.
Every completed task needs its evidence source IDs. Defer an unfinished task explicitly
in the handoff. Use `WAIT` for an affected action with missing required evidence.

Create proposed orders with `python -m stock_jp operation orders propose` after the
research is complete. Use the policy-derived status; PAPER tickets are `PAPER_PROPOSED`.
Reconcile human-reported executions through the trade and capital ledgers before adding
another ticket. Brokerage submission is `HUMAN_ONLY`.

## Close and report

Finalize with `python -m stock_jp operation run finalize` and the same token only when
all required coverage and artifacts pass validation. A failed run preserves its partial
files and does not advance the successful disclosure cutoff.

Report the status, next-session actions, proposed tickets, human tasks, data gaps,
queued reviews, saved artifact paths, and next scheduled run. After a successful run,
stop until the next scheduled review. PAPER requires no morning order submission.

## Publication

Follow `docs/operations/automation.md`. Validate the PR with
`python -m stock_jp automation publication`. Verify the allowed paths, base/head,
semantic integrity, and `format`, `test`, `guardrail` checks before using an authorized
publication flow. Market data and daily results remain in this private repository.
Code, policy, configuration, and financial-ledger changes require normal review.
A data acquisition is separate from a completed nightly run. Preserve partial outcomes
and consumed budgets; never describe a preservation PR as a completed run.
