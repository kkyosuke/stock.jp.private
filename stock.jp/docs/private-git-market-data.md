# Private Git market-data storage

現行の正本は単一private repo stock.jp.privateです。stock.jp/は通常フォルダです。
CLIのpack/restore例のdata/はstock.jp/基準、stageの--rootはrepo rootです。
新規licensed uploadの既存拒否はこの構成変更で解除されず、再試行・迂回しません。

The owner consolidated common code and existing committed data into the private
`kkyosuke/stock.jp.private` repository. Market data lives under `stock.jp/data/`;
account state, orders and ledgers live under `operations/private/`. Credentials
remain outside Git. The original repository and its history are preserved.
Newly acquired samples are excluded from the migration; the outstanding upload
rejection remains in force.

## Destination and access

The exact archive destination is `stock.jp/data/jquants-archive/` relative to
the root of `kkyosuke/stock.jp.private`. GitHub metadata must show private
visibility and push access immediately before each data push. The owner must
confirm that only they can view this repository; private visibility alone does
not enumerate collaborators, apps, forks or backups. A confirmation is owner
attestation, not an independently audited ACL. If visibility becomes public or
access cannot be checked, do not stage or push licensed data. Do not change
visibility or request new credentials to get around a failed check.

[Provider usage conditions](https://jpx-jquants.com/ja/help/usage) allow owner-managed
cloud storage when only the subscriber can view it. Cancellation or downgrade
requires deletion of affected records, copies and reversible derivatives. Git
history, branches, tags, PR refs, forks, clones and backups are copies too: a
normal deletion commit is insufficient. Inventory them and obtain separate
approval for destructive history rewriting/purging and any GitHub support work.
No history purge, subscription change, purchase or new terms acceptance is part
of this implementation.

## Format and bounds

Uncompressed acquisition remains in the ignored `operations/private/market-data/`
work area, including its resumable page checkpoints and normalized daily prices.
The existing private-path resolver and readiness gates are unchanged. Restore
into that work area explicitly; never fall back to legacy `data/daily-prices/`.

New writes use version 2: complete original JSON and CSV bytes are stored under
`objects/<prefix>/<sha256>.json` or `.csv`. The generation index maps the original
logical filenames to those objects and retains each file's SHA-256 and byte count.
No JSON-to-CSV conversion or pretty-printing changes the original bytes. Non-JSON/
CSV binary cache members, if encountered, remain lossless `.bin` objects; this
migration contains only JSON and CSV. Indexes are readable JSON under
`generations/<prefix>/<sha256>.json`; `current.json` selects a fully validated index.

Whole files are limited to 25 MiB and the store to 750 MiB. Oversized files stop
before pointer publication; they are not silently truncated, split into invalid
JSON, or converted to a lossy format. Identical objects are reused. Existing
objects and indexes are immutable during ordinary daily publication. The explicit
format-migration PR replaces the working tree representation after verifying every
old generation; it does not rewrite Git history.

Version-1 gzip/chunk archives remain readable. New writes, including captures by
the legacy Actions adapter, use version 2. Git checkpoint transport may still split
complete stored files into 1 MiB Git blobs; this is distinct from the on-disk format.
The deleted legacy checkpoint branch is not recreated by this change.

Packing holds the collector's local archive lock. Use only one runtime/writer;
this is not a distributed lock. Git non-fast-forward failures stop publication.
Restore validates legacy compressed or new plain hashes, size bounds, relative paths and
complete file hashes into a new directory, then renames it into place. It refuses
symlinks and existing targets, preserving any active checkpoints. Credentials and
signed URL patterns are rejected without reading runtime secrets. No codec
command performs API acquisition, uploads, commits, trades or state migration.

Nightly page checkpoints are included under `nightly-source-cache/<scope-hash>/`.
Only hashed `pages/<query-hash>.json` receipts and
`objects/<prefix>/<content-hash>.json` payloads are accepted. Packing locks the
existing cache scopes as well as the ingestion archive. Lock files are not stored.
Both packing and restoration validate the Light-plan fixed origin, supported
nightly endpoint/query shapes, aware cutoff/retrieval timestamps, scope/query
hashes, exact receipt fields, object paths, lengths, hashes and response schema.
Missing or unreferenced payloads fail validation; checkpoint contents are never
rewritten or discarded to make an archive pass. Old archives without this cache
remain compatible. Hash validation checks internal consistency, not a provider
signature or permission to publish. Replaying restored pages still requires the
exact original cutoff and query; it does not constitute a fresh source check.

[GitHub file guidance](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)
warns above 50 MiB, blocks above 100 MiB, and recommends repositories below 1 GB
(under 5 GB strongly recommended). The
[repository limits](https://docs.github.com/en/repositories/creating-and-managing-repositories/repository-limits)
also document a 2 GB push limit and a 10 GB on-disk recommendation. These are
not guarantees that a database-sized history is suitable. This implementation
uses a conservative 750 MiB cumulative store cap; include existing Git history,
future correction generations and index growth when sizing. Never solve a size
failure by omitting a dataset or buying LFS. Stop and report unsuitable size.

## Acquisition, validation and publication

Set `jquants.ingestion.max_requests_per_run` to a finite positive value (initially
12). Keep plan Light, no add-ons. From the vendored stock.jp/ directory:

```sh
python scripts/jquants_ingest.py
```

Exit 2 with `pending_resume` is a saved partial batch, not nine-dataset completion.
Resume the same command after inspecting the aggregate report. The first batch
runs the existing recent master/OHLC preflight. Sample each entitled dataset
before extrapolating full-period volume: a master-only sample cannot estimate
financial statements or daily bars. Run `python scripts/jquants_size_sample.py`
after the preflight to save an aggregate one-date sizing sample with a separate
finite budget. Include unknown/unavailable/error datasets
in the sizing report and stop before an unbounded backfill.

Test locally using an ignored sibling store; no raw rows are printed:

```sh
python scripts/market_data_git.py pack \
  --source ../operations/private/market-data \
  --store ../operations/private/market-data-staging/codec
python scripts/market_data_git.py restore \
  --store ../operations/private/market-data-staging/codec \
  --target ../operations/private/market-data-staging/roundtrip
```

Save sanitized repository metadata from the authorized GitHub read tool in the
ignored staging directory, including `checked_at` in UTC, repository_full_name,
visibility, owner.login and permissions.push. `stage` rejects missing, public,
wrong-destination or older-than-five-minute metadata. Repeat this check before
push; do not fabricate a timestamp without a fresh metadata read.

After offline tests, actual roundtrip, sizing, fresh private visibility/push
metadata, and owner-only confirmation, `stage` writes the approved tracked store:

```sh
python scripts/market_data_git.py stage --metadata ../operations/private/market-data-staging/repository.json
python scripts/market_data_git.py verify --store data/jquants-archive
```

Review the complete diff for credential patterns, and verify that only market
data and codec indexes enter `data/jquants-archive/`. No plaintext raw rows in PR
text/logs. Commit and push on a dedicated daily data branch with a non-draft PR,
rechecking
private visibility immediately before push. Record the remote SHA before saying
saved; a local commit is not durable cloud storage. No main merge is automatic.
Eligible daily PRs use native auto-merge after exact-head CI and semantic checks,
following `daily-pr-publication.md`; required reviews remain enforced. Code and
configuration changes remain ordinary draft PRs. A queued PR is not yet adopted.
The codec cannot prevent someone from
using Git directly or changing repository visibility after upload; the operator
must maintain access controls and perform the publication preflight every time.

Actions use sparse working trees excluding licensed generations, run synthetic
tests only, and never restore, upload, cache or publish the archive. Git transport
may still fetch repository objects; no claim is made that sparse checkout keeps
objects out of `.git`. Do not grant third parties Actions logs or repository access.
Daily scheduling and full nightly/PAPER success remain separate work.
