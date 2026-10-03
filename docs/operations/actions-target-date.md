# Date-specific acquisition Action (disabled)

The `daily-collector-disabled.yml` Action accepts one required `target_date` in
`YYYY-MM-DD` format. Its job remains guarded by unconditional `false`, with no
schedule. This code change neither selects an operator nor enables acquisition.
The legacy history workflow also remains disabled. No controller service is added.

## Interface and date semantics

The common entry point is:

```sh
python stock.jp/scripts/actions_target_driver.py --target-date "$STOCK_TARGET_DATE" --root "$GITHUB_WORKSPACE"
```

Workflow inputs enter through a quoted environment variable, never interpolated
into shell source. Invalid or future dates are rejected. The current JST day uses
the existing daily plan, including provisional financial disclosure repair. A
previously started date retains its original profile when resumed later. A new
past date uses only the approved history range: dated equities master and bars,
plus calendar and TOPIX shared across the whole campaign. It does not claim all
Light datasets are complete. No arbitrary dataset/profile override is exposed.

The approved history plan contains `start_date`, `as_of` and `sessions`. The range
is at most 550 elapsed calendar days and 280 business sessions. Calendar coverage
must be complete and its session count must match the plan. Missing business dates
are processed oldest first, one date per invocation; a date must finish publication
and restore before another starts. A verified holiday needs no price fetch. Empty
required business-day prices pause instead of becoming a successful holiday.

`target_date` is the market/disclosure date, not an execution clock. Journal start,
request reservations and raw retrieval times record actual execution time. Data
retrieved now for a past date is not evidence of what was known then. Dated master,
market membership, unadjusted prices and separate adjusted fields retain existing
normalization rules. Daily financial provisional/final-window receipts remain
unchanged; past-date history does not add financial or recent-only datasets.

## Durable units and bounds

- One journal and persistent maximum of 12 provider requests per target date.
  The existing daily plan's older financial repairs consume that journal's budget.
- One approved history campaign: at most 600 requests and 50 batches of 12.
  A batch spans dates and workflow invocations. Each history reservation belongs
  to both its date journal and its campaign batch. Pagination and retries count.
- Raw objects and dataset revisions remain immutable content-addressed records.
  Shared range queries, raw page hashes and pagination progress live in the ledger.
  A validated `jquants/target-history/ranges.json` sidecar preserves their provenance
  and actual retrieval timestamps in the data archive as well.
- One data PR/publication transaction per date, followed by verified restoration.
  The existing codec, data-only scope, CI/review gates and SHA-guarded merge remain.
- Same-date completion is a verified no-op. Closed/rejected old PRs on the same ref
  cannot be mistaken for a fresh publication. An unresolved intent stops for review.

A 280-session history with one page per request needs 562 requests and 47 batches:
560 dated master/bars requests plus two shared range requests. Extra pages/retries
use the remaining 38 requests. Repeating the full daily plan for 280 dates cannot
fit this cap. Switching dates, rerunning, changing the manifest or deleting the
checkpoint cannot replenish budget. Unknown outcomes remain charged and blocked.
All existing contract, coverage, resource, GitHub pacing and PAPER/HUMAN_ONLY limits
remain in force. Approval renewal/rebinding requires a separately reviewed transition.

## Activation prerequisites (not performed by this change)

Before activation, choose the operator and review credentials/permissions through
the existing repository mechanism. The runtime uses only the existing GitHub and
J-Quants transports. This change creates no credentials or secret settings.

A separately reviewed version-2 approval must retain the existing policy, baseline,
limits and owner approval PR/comment fields, and add:

- `assurance: retired-external-cooperative-github`
- a stable `activation_id` and `retirement_sha256`
- `checkpoint_initial_sha`: the reviewed initial checkpoint commit
- both `daily` and `history` modes and the explicit history range above

Prepare and inspect the initial checkpoint using the existing checkpoint tooling
under separate authorization, before pinning its SHA in the approval. Acquisition
never bootstraps a missing ref. An initial checkpoint may preserve already adopted
completed journals; it cannot import unresolved or rejected campaigns. Subsequent
commits must descend from that SHA and retain the approval binding and budgets.
No genesis or activation record is created by this PR. An adopted legacy history
campaign may be preserved for daily use, but starting target-history work from it
requires separately reviewed budget mapping; the driver will not grant a fresh
600-request allowance automatically.

The parent must first stop the old schedule and confirm all prior cloud writers
have terminated without queued resumes. Store the real observation in
`.github/stock-automation-retirement.json` using this schema (placeholders below
are documentation, not usable evidence):

```json
{
  "version": 1,
  "repository": "kkyosuke/stock.jp.private",
  "activation_id": "REVIEWED_ACTIVATION_ID",
  "observed_by": "kkyosuke",
  "observed_at": "ACTUAL_TIME_WITH_TIMEZONE",
  "scheduler": {"id": "ACTUAL_TASK_ID", "enabled": false},
  "cloud_writers": [
    {"id": "ACTUAL_RUN_ID", "terminated": true, "queued_resume": false}
  ],
  "evidence": [
    {"reference": "OWNER_REVIEWED_EVIDENCE_REFERENCE", "sha256": "ACTUAL_EVIDENCE_SHA256"}
  ]
}
```

The verifier checks the receipt hash, identity, schema, nonfuture observation time,
reviewed manifest and adopted owner approval. Evidence references are the owner's
attestation: the Action cannot independently inspect ChatGPT task state or cloud
process termination. Never refresh the retirement timestamp or fabricate a lease.
The old schedule has **not** been stopped as part of this code work.

Current ownership is separate: use GitHub's workflow run attempt GET endpoint,
plus a durable `run_id`/`run_attempt`/head SHA/activation owner and CAS checkpoint.
Transfer requires the previous attempt to be `completed`; elapsed time or lease TTL
alone never authorizes takeover. Current run status and checkpoint ownership are
rechecked before provider I/O and before publication, with bounded-age checks after
pacing. Same-repository workflow concurrency coordinates cooperating writers.
These controls cannot technically prevent an arbitrary external writer retaining
valid credentials from restarting; that is outside this assurance model.

Existing `publication_rejections`, `unresolved_campaigns`, blocked ledgers, unknown
reservations and pending publication intents continue to block activation/resume.
They are not deleted, transferred, relabeled as approved or silently quarantined.
An operator must separately review their disposition; this PR does not resolve them.

## Offline verification

`test_actions_target_driver.py` exercises the real driver, collectors, codec and
Git object publication/restore using synthetic GitHub and provider transports.
It covers shared batches, repeat dates, holidays, date mismatch, current daily
behavior, owner attempts/termination, retained unknown outcomes, manifest resets,
old same-ref PRs, missing retirement/checkpoint evidence and persistent budgets.
The 600/50 boundary is also checked directly against the durable reservation shape.
Legacy send-boundary and collector suites remain applicable. These tests do not
prove credential scope, external retirement or live provider readiness.
