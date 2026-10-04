# J-Quants V2: private full-history ingestion

現行の正本は単一private repo stock.jp.privateです。stock.jp/は通常フォルダです。
CLIのpack/restore例のdata/はstock.jp/基準、stageの--rootはrepo rootです。
新規licensed uploadの既存拒否はこの構成変更で解除されず、再試行・迂回しません。

## Scope and prerequisites

The provider is J-Quants API V2. `scripts/jquants_catalog.py` explicitly lists all
31 documented resource/delivery endpoints as reviewed on 2026-10-01. It does not
infer a purchased plan from a payment amount. The public template leaves
`jquants.plan` unset; the operator must verify it. Supported plan declarations
are `free`, `light`, `standard`, `premium`. Add-ons are separately declared as
`minute_tick` and/or `tdnet`; no subscription is purchased or upgraded by code.

Light's collection scope is nine datasets: point-in-time securities master,
daily equity OHLCV, valuation, financial summaries, historical earnings dates,
recent earnings calendar, trading calendar, investor-type statistics, and
TOPIX daily prices. It requests all issues, not just a watchlist. The first
history pass covers the plan's full rolling five-year window (or the dataset's
later inception), including calendar dates with zero results. The calendar is
also collected through the end of the next year. Latest-only datasets are
explicitly identified and cannot be backfilled beyond the provider's offering.
Other datasets remain listed with `not_entitled` based on the declared plan.
A generic HTTP 403 is an error, not proof that a dataset is outside the plan.

Before a live run:

1. Use an owner-controlled private work area and the explicitly configured
   private Git destination described in `private-git-market-data.md`; temporary
   local acquisition is allowed, but is not durable until remote verification
2. Revoke any key exposed in chat or logs, then register its replacement using
   the runtime's secure secret mechanism as `JQUANTS_API_KEY`. Never paste it
   into chat, a command, tracked configuration, examples, or a fixture
3. Ensure `operations/private` is the approved private workspace. Both raw and
   normalized paths must be descendants of its `market-data` directory
4. Set the confirmed plan in the private source configuration. There is no
   automatic higher-tier or add-on entitlement inference

A GitHub Actions repository secret is not readable by an external collector.
Configure the key in the actual runtime that runs the collector. Public Actions
are fixture-only guardrails: they neither collect licensed data nor publish it.
The owner explicitly selected the now-private `kkyosuke/stock.jp.private` repository
for compressed archive generations under `data/jquants-archive/`. Only this
reviewed Git destination is allowed. Do not put raw records in Actions artifacts,
Pages, public repositories/buckets, third-party AI inputs, or training datasets. Private repositories and cloud
storage are not automatically a license to share licensed data. Keep access
owner-only and verify any external hosting or backup arrangement separately.

## Running on the authorized private runtime

From the private code/data checkout linked to the private workspace:

```sh
python scripts/jquants_ingest.py --catalog
python scripts/jquants_ingest.py
```

The catalog command is metadata-only and performs no network request. Collection
fails closed without the fixed environment variable. The API origin is fixed at
`https://api.jquants.com/v2`; the key is never put in query strings or persisted.
The run creates a recent daily snapshot first, then continues the historical
backfill. It does not change PAPER/HUMAN_ONLY policy or place any order.

Initial collection can take hours because full-market responses may have many
pages. Requests are serial and conservatively paced at no more than 60/minute
(5/minute for Free), including the endpoints with dedicated 60/minute limits.
HTTP 429 waits at least two minutes before a bounded exponential retry; transient
network/5xx errors also retry. No redirects can forward the API credential.

`jquants.ingestion.max_requests_per_run` can bound runtime API requests. A budget
exit returns status 2 and `pending_resume`, not success. Re-running the same
command resumes durable pages/partitions, even when a multiday initial backfill
crosses into a new date. Use one process per archive. Concurrent acquisition in
another program/account session still shares the provider rate quota.

## Storage and correction semantics

- Raw: `operations/private/market-data/jquants/`
- Normalized: `operations/private/market-data/daily-prices/`
- Separate sibling directories prevent daily health checks from copying the
  entire historical raw dataset
- `objects/<prefix>/<sha256>.json`: immutable complete response pages, including
  unknown fields, nested EDINET structures, raw/adjusted values, and nulls
- `datasets/<dataset>/<date>/checkpoint.json`: durable in-progress page list and
  continuation token; no partition is published while pagination is incomplete
- `datasets/<dataset>/<date>/revisions/<sha256>.json`: immutable partition
  manifests with endpoint, query, date range, retrieval time, units, page hashes,
  record counts and full-universe scope
- Per-partition `latest.json`: latest fully acquired revision only
- Dataset `state.json`: completed partitions and reconciliation-cycle progress
- Root `latest.json` and immutable `runs/*.json`: every catalog dataset's status,
  requested/observed coverage, errors, and `all_entitled_complete`

Interrupted acquisition cannot replace the prior complete partition. Corrections
produce new content-addressed pages and versioned manifests rather than deleting
older evidence. Every resume validates stored page checksums. Recent 30 calendar
days are re-read on later runs; a complete retained-window reconciliation occurs
every 30 days to catch corrections older than the overlap. These are defaults,
not a guarantee that the vendor cannot revise older records between passes.
Data outside a shrinking provider window remains stored until the owner removes
it under applicable license obligations; retention expiry never silently deletes
local history. Files are private-mode and may be deleted locally when no longer
needed. Restoring/deleting the entire raw archive starts a new backfill.

`all_entitled_complete` means every requested accessible dataset partition was
acquired and validated, including observed empty responses. It is distinct from
trade readiness and does not assert that the provider has a row for every stock
on every calendar date. Endpoint delivery mechanisms (bulk list/get) are cataloged
but are not duplicated as data when equivalent complete JSON is collected.
A subset run or unsupported/unavailable/error partition cannot claim full scope.

## Snapshot and operational evidence

`jquants_snapshot.py` exports same-date master/daily-bars records using unadjusted
`O/H/L/C/Vo` (JPY, shares). `Adj*` fields remain separately named in raw storage;
no adjusted value is mislabeled as an execution price. The master uses product
category `011` and common-share reserve code `0` to derive a point-in-time
domestic ordinary-stock universe, including historical delistings. Alphanumeric
issue codes are retained. Missing/bad quotes reduce coverage instead of dropping
stocks from the denominator.

Daily CSVs and immutable revisions use the established Japanese-column contract.
The latest manifest identifies J-Quants V2 and the unadjusted basis, with raw
immutable provenance and checksums. An old backfill date never regresses the
latest snapshot. Existing freshness, whole-universe coverage, active-target,
checksum, risk, event-review, and human approval gates remain in force. Archived
Yahoo data can be validated only under its explicit legacy provider identity;
it is never relabeled as official J-Quants or used as a fallback.

Operational official-source collection uses the same fixed-origin paced client.
The full archive is an acquisition/evidence layer; financial statements and
other data do not automatically become verified investment conclusions.

## Verification and limitations

Tests use synthetic fixtures only. They cover paging/resumption, corrupt objects,
retries, error classification, revision retention, subscription scope, snapshot
identity, wrong dates, invalid OHLCV, alphanumeric codes and path confinement.
No actual user API key, account entitlement response, or paid dataset is needed
for CI. Live account authentication, vendor-response schema compatibility at
runtime, completion of the initial multi-year download, license-compliant
hosting, available disk capacity and an external runtime schedule must be
verified separately. Add-on signed-file hosts require explicit reviewed host
configuration; no host is guessed from the base-plan subscription.

## Official references

- [Subscription coverage](https://jpx-jquants.com/en/spec/data-spec)
- [API reference catalog](https://jpx-jquants.com/en/spec)
- [Pagination](https://jpx-jquants.com/en/spec/pagination)
- [Rate limits](https://jpx-jquants.com/en/spec/rate-limits)
- [Response status](https://jpx-jquants.com/ja/spec/response-status)
- [Product categories](https://jpx-jquants.com/en/spec/eq-master/product-category)
- [Data FAQ](https://jpx-jquants.com/en/help/data)
- [Official Python client](https://github.com/J-Quants/jquants-api-client-python)
