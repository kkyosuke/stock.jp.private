"""Local fail-closed gate for assistant-owned daily PRs; never pushes or merges."""
from __future__ import annotations
import argparse
import csv
from datetime import date, datetime, timezone
import io
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import shutil

try:
    from scripts import market_data_git as codec
except ModuleNotFoundError:
    import market_data_git as codec

REPOS = {'market': 'kkyosuke/stock.jp.private', 'personal': 'kkyosuke/stock.jp.private'}
CHECKS = {kind: {'format', 'test', 'guardrail'} for kind in REPOS}
WORKFLOWS = {kind: {'.github/workflows/format-check.yml', '.github/workflows/operation-tests.yml',
                    '.github/workflows/daily-stock-prices.yml'} for kind in REPOS}
ARTIFACTS = {'work-plan.json', 'sources.csv', 'research-results.md', 'research-queue.json',
             'report.md', 'provider-health.json', 'pretrade-check.md', 'orders.csv',
             'next-day-actions.csv', 'lease.json', 'handoff.json', 'global-risk.md',
             'coverage.json', 'assessment-status.json'}
STATE_FIELDS = {'state_revision', 'last_successful_run_at_jst', 'last_disclosure_cutoff_jst',
                'last_price_date', 'last_run_id', 'pending_reviews', 'pending_orders',
                'data_gaps', 'next_run_at_jst', 'unreconciled_ticket_ids', 'consecutive_successful_runs'}


class Blocked(ValueError):
    pass


def require(value, reason):
    if not value:
        raise Blocked(reason)


def git(root, *args):
    try:
        from scripts.git_sandbox import git as safe_git
    except ModuleNotFoundError:
        from git_sandbox import git as safe_git
    return safe_git(root,*args)


def changes(root, base, head):
    for sha in (base, head):
        require(re.fullmatch('[0-9a-f]{40}', sha), 'explicit commit SHA required')
    require(git(root, 'merge-base', base, head).decode().strip() == base, 'rebase on current main first')
    result = []
    for line in git(root, 'diff', '--no-renames', '--raw', base, head).decode().splitlines():
        metadata, path = line.split('\t')
        oldmode, mode, _, _, status = metadata[1:].split()
        require(status in {'A', 'M'} and mode == '100644' and oldmode in {'000000', '100644'},
                'deletions, renames, executable files and symlinks require review')
        result.append({'path': path, 'status': status})
    require(result, 'empty daily change')
    return result


def classify(kind, run_id, entries):
    require(kind in REPOS, 'unsupported publication kind')
    require(date.fromisoformat(run_id).isoformat() == run_id, 'invalid run date')
    run_prefix = f'operations/private/runs/{run_id}/'
    for entry in entries:
        path, status = entry['path'], entry['status']
        if kind == 'market':
            require(path.startswith('stock.jp/data/jquants-archive/') and codec.store_member(path.removeprefix('stock.jp/data/jquants-archive/')),
                    'unexpected market path')
            require(path.endswith('/current.json') or status == 'A', 'immutable archive object changed')
        else:
            allowed = path in {'operations/private/state.json', 'operations/private/run-history.csv', 'operations/private/source-watermarks.json', 'operations/private/trade-event-ledger.csv', 'operations/private/market-regime-log.csv'}
            if path.startswith(run_prefix):
                name = path[len(run_prefix):]
                allowed = name in ARTIFACTS or bool(re.fullmatch(r'investment-cases/[0-9A-Z]{4,5}\.json', name))
            if re.fullmatch(r'operations/private/decisions/' + run_id + r'-[0-9A-Z]{4,5}-[a-z0-9-]+\.md', path):
                allowed = status == 'A'
            if re.fullmatch(r'operations/private/research-inputs/market-regime/[0-9]{4}-[0-9]{2}-[0-9]{2}\.json', path):
                allowed = status == 'A'
            require(allowed, 'unexpected personal path; config, code, policy, financial ledgers and prior runs require review')
    if kind == 'market':
        require(any(x['path'] == 'stock.jp/data/jquants-archive/current.json' for x in entries), 'generation pointer required')
    else:
        required = {run_prefix + 'handoff.json', 'operations/private/state.json', 'operations/private/run-history.csv'}
        require(required <= {x['path'] for x in entries}, 'run, state and append-only history must publish together')


def materialize(root, sha, prefix, target):
    for line in git(root, 'ls-tree', '-rz', sha, '--', prefix).split(b'\0'):
        if not line:
            continue
        metadata, rawpath = line.split(b'\t')
        mode, kind, _ = metadata.split()
        require(mode == b'100644' and kind == b'blob', 'unsafe stored file mode')
        path = rawpath.decode()
        require('..' not in Path(path).parts, 'unsafe stored path')
        data = git(root, 'show', f'{sha}:{path}')
        codec.scan(data) if not path.endswith('.gz') else None
        out = target / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)


def read_json(root, sha, path):
    return json.loads(git(root, 'show', f'{sha}:{path}'))


def strict_metadata(data):
    codec.scan(data)
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate archive metadata field')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique_pairs)


def validate_added_archive_members(store, entries):
    """Every new stored byte must belong to the fully validated current generation.

    Older generations already in base are retained, never rewritten or deleted.
    A daily PR publishes one generation; intermediate/orphan local files stay local.
    """
    pointer = strict_metadata((store / 'current.json').read_bytes())
    require(set(pointer) == {'version', 'generation', 'file_count', 'raw_bytes'}, 'unexpected pointer field')
    require(type(pointer['version']) is int and pointer['version'] in {1,2}
            and type(pointer['file_count']) is int and type(pointer['raw_bytes']) is int,
            'invalid pointer schema')
    manifest = strict_metadata(codec.manifest_bytes(store, pointer))
    require(set(manifest) == {'version', 'files'} and type(manifest['version']) is int
            and manifest['version'] == pointer['version'] and isinstance(manifest['files'], dict), 'unexpected generation schema')
    for entry in manifest['files'].values():
        fields = {'sha256', 'bytes', 'chunks'} if pointer['version'] == 1 else {'sha256', 'bytes', 'object'}
        require(isinstance(entry, dict) and set(entry) == fields, 'unexpected file metadata')
    reachable = codec.reachable_members(store, pointer)
    for entry in entries:
        if entry['status'] == 'A' and entry['path'] != 'stock.jp/data/jquants-archive/current.json':
            name = entry['path'].removeprefix('stock.jp/data/jquants-archive/')
            require(name in reachable, 'unreferenced added archive payload')
    # restore subsequently verifies every referenced plain or legacy compressed hash/name, shard
    # bound, expanded-size bound and reconstructed file checksum/content.


def market_semantics(root, head, scratch, entries):
    materialize(root, head, 'stock.jp/data/jquants-archive', scratch)
    store = scratch / 'stock.jp/data/jquants-archive'
    require(sum(p.stat().st_size for p in store.rglob('*') if p.is_file()) <= codec.MAX_STORE, 'store budget exceeded')
    validate_added_archive_members(store, entries)
    codec.restore(store, scratch / 'restored')
    for path in (scratch / 'restored').rglob('*'):
        if not path.is_file():
            continue
        name = path.relative_to(scratch / 'restored').as_posix()
        allowed = (codec.nightly_cache.valid_name(name)
                   or re.fullmatch(r'jquants/(latest|size-sample)\.json', name)
                   or re.fullmatch(r'jquants/objects/[0-9a-f]{2}/[0-9a-f]{64}\.json', name)
                   or re.fullmatch(r'jquants/runs/[0-9TZ]+\.json', name)
                   or name in {'jquants/history/state.json', 'jquants/history-marker.json', 'jquants/target-history/ranges.json'}
                   or re.fullmatch(r'jquants/datasets/[a-z_]+/(state\.json|[0-9-]{10}/(latest\.json|checkpoint\.json|revisions/[0-9a-f]{64}\.json))', name)
                   or re.fullmatch(r'daily-prices/(latest\.json|[0-9]{4}/[0-9-]{10}\.csv|revisions/[0-9-]{10}/[0-9a-f]{64}/([0-9-]{10}\.csv|manifest-[0-9a-f]{64}\.json))', name))
        require(allowed, 'unexpected restored market member')
        if path.suffix == '.json':
            def reject_personal(value):
                if isinstance(value, dict):
                    require(not (set(value) & {'account_number', 'broker_account', 'holdings', 'watchlist', 'portfolio', 'cash_balance', 'order_id', 'personal_analysis'}), 'personal field in shared market data')
                    for child in value.values(): reject_personal(child)
                elif isinstance(value, list):
                    for child in value: reject_personal(child)
            reject_personal(json.loads(path.read_text()))
            value = json.loads(path.read_text())
            if re.fullmatch(r'jquants/runs/[0-9]{8}T120000Z\.json', name):
                try:
                    from scripts.jquants_daily import validate_state
                except ModuleNotFoundError:
                    from jquants_daily import validate_state
                validate_state(value)
                require(Path(name).name == value['run_date'].replace('-', '') + 'T120000Z.json',
                        'daily journal filename does not match run date')
    report = json.loads((scratch / 'restored/jquants/latest.json').read_text())
    require(report.get('provider') == 'J-Quants API V2' and report.get('declared_plan') == 'light'
            and report.get('declared_addons') == [], 'unexpected subscription/provider')
    rows = report.get('datasets', {})
    required = {'equities_master', 'equities_daily', 'equities_valuation', 'financial_summary',
                'earnings_dates', 'earnings_calendar', 'trading_calendar', 'investor_types', 'topix_daily'}
    require(required <= rows.keys(), 'missing entitled dataset status')
    require(all(rows[n].get('status') == 'supported' and rows[n].get('collection_status') in
                {'complete', 'pending_resume', 'pending'} for n in required), 'dataset error or unclassified state')
    complete = all(rows[n]['collection_status'] == 'complete' for n in required)
    require(report.get('all_entitled_complete') is complete, 'inconsistent completeness claim')
    history_path = scratch / 'restored/jquants/history/state.json'
    history_status = json.loads(history_path.read_text())['status'] if history_path.exists() else None
    return {'scope': 'complete_acquisition' if complete else 'partial_acquisition',
            'operational_readiness': False, 'history_status': history_status}


def validate_proposal_events(old_bytes, new_bytes, orders, run_id):
    require(new_bytes.startswith(old_bytes), 'trade event history rewrite rejected')
    fields = next(csv.reader(io.StringIO(old_bytes.decode())))
    events = list(csv.DictReader(io.StringIO(new_bytes[len(old_bytes):].decode()), fieldnames=fields))
    require(len(events) == len(orders), 'one appended proposal event per new ticket required')
    indexed = {row['ticket_id']: row for row in orders}
    require(len(indexed) == len(orders), 'duplicate proposal tickets')
    seen = set()
    allowed_values = {'event_id', 'occurred_at_jst', 'trade_date', 'ticket_id', 'decision_id', 'code', 'side',
                      'event_type', 'operation_mode', 'rule_version', 'order_type', 'limit_price',
                      'quantity_private', 'evidence_path', 'notes'}
    for event in events:
        ticket = event.get('ticket_id')
        require(ticket in indexed and ticket not in seen, 'unmatched or duplicate proposal event')
        seen.add(ticket); order = indexed[ticket]
        require(event.get('event_type') == 'PAPER_PROPOSED' and event.get('operation_mode') == 'PAPER'
                and order.get('status') == 'PAPER_PROPOSED', 'only virtual proposals may append automatically')
        require(all(not v for k,v in event.items() if k not in allowed_values), 'fill, fee, broker or capital values rejected')
        require(event.get('event_id') == ticket + '-PAPER_PROPOSED'
                and event.get('evidence_path') == f'operations/private/runs/{run_id}/orders.csv'
                and event.get('occurred_at_jst') == order.get('prepared_at_jst'), 'proposal provenance mismatch')
        for key in ('trade_date', 'decision_id', 'code', 'side', 'rule_version', 'order_type', 'limit_price', 'quantity_private'):
            require(event.get(key) == order.get(key), 'proposal event differs from ticket')


def validate_market_regime_append(root, base, head, run_id, scratch, entries, handoff):
    """Replay a generated MRS append using committed inputs and local prices.

    Price files are read only from committed source-config's explicit private
    archive, copied into isolation, and fingerprinted into the validation proof.
    No collector/network fallback runs here. Missing evidence fails closed.
    """
    prefix = 'operations/private/research-inputs/market-regime/'
    log_name = 'operations/private/market-regime-log.csv'
    added_inputs = [x['path'] for x in entries if x['path'].startswith(prefix)]
    log_changed = any(x['path'] == log_name for x in entries)
    if not added_inputs and not log_changed:
        return None
    require(len(added_inputs) == 1 and log_changed, 'one generated MRS input and log append must publish together')
    try:
        from scripts.market_data_storage import resolve_archive_root
        from scripts.market_regime import derive_market_regime
        from scripts.assessment_pipeline import _append_mrs_log, _existing_mrs
    except ModuleNotFoundError:
        from market_data_storage import resolve_archive_root
        from market_regime import derive_market_regime
        from assessment_pipeline import _append_mrs_log, _existing_mrs
    config = read_json(root, base, 'operations/private/source-config.json')
    archive = resolve_archive_root(root, config)
    sessions = []
    first = date.fromisoformat(run_id).replace(day=1)
    for path in archive.glob('*/*.csv'):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day < first:
            require(path.parent.name == str(day.year) and not path.is_symlink(), 'unsafe MRS price evidence')
            sessions.append((day, path))
    sessions.sort()
    require(sessions, 'MRS price evidence missing')
    target = sessions[-1][0].isoformat()
    require(added_inputs == [prefix + target + '.json'], 'MRS input must target latest archived prior-month session')
    raw = strict_metadata((scratch / added_inputs[0]).read_bytes())
    require(raw.get('as_of') == target, 'MRS input date mismatch')
    defaults = {'minimum_vi_observations': 500, 'minimum_breadth_coverage': 0.98,
                'breadth_lookback_sessions': 280, 'maximum_breadth_close_age_days': 7}
    require(set(raw) <= {'schema_version', 'as_of', 'series'} | defaults.keys(), 'unexpected MRS input field')
    for key, default in defaults.items():
        require(raw.get(key, default) == config.get('market_regime', {}).get(key, default),
                'MRS evidence may not override configured risk thresholds')
    require(set(raw.get('series', {})) == {'topix', 'growth250', 'nikkei_vi', 'leading_ci'}, 'unexpected MRS series')
    snapshot = scratch / 'mrs-price-evidence'
    hashes = {}
    lookback = int(raw.get('breadth_lookback_sessions', 280))
    require(lookback >= 200, 'invalid MRS lookback')
    for _, path in sessions[-lookback:]:
        relative = path.relative_to(archive)
        require(path.resolve().is_relative_to(archive), 'MRS price evidence escapes archive')
        data = path.read_bytes()
        codec.atomic(snapshot / relative, data)
        hashes[relative.as_posix()] = hashlib.sha256(data).hexdigest()
    values, result, derived = derive_market_regime(raw, archive_root=snapshot)
    assessment = json.loads((scratch / f'operations/private/runs/{run_id}/assessment-status.json').read_text())
    calculated_at = assessment['calculated_at_jst']
    require(datetime.fromisoformat(handoff['started_at_jst']) <= datetime.fromisoformat(calculated_at)
            <= datetime.fromisoformat(handoff['completed_at_jst']), 'MRS calculation outside current run')
    expected_status = {'status': 'CURRENT', 'as_of': target, 'state': result.state,
                       'score': result.score, 'entry_multiplier': result.entry_multiplier,
                       'components': result.components, 'derived': derived,
                       'input_path': added_inputs[0], 'log_recorded': True}
    require(assessment['market_regime'] == expected_status, 'MRS assessment differs from recalculation')
    before = git(root, 'show', f'{base}:{log_name}')
    actual = (scratch / log_name).read_bytes()
    require(actual.startswith(before), 'MRS risk history rewrite rejected')
    expected = scratch / 'expected-mrs-log.csv'
    expected.write_bytes(before)
    require(_existing_mrs(expected, target) is None, 'MRS historical correction requires review')
    urls = list(dict.fromkeys(str(series['source_url']) for series in raw['series'].values()
                             if isinstance(series, dict) and series.get('source_url')))
    _append_mrs_log(expected, values=values, result=result, source_urls=urls, calculated_at=calculated_at)
    require(actual == expected.read_bytes(), 'MRS log differs from canonical append')
    return {'as_of': target, 'price_evidence_sha256': hashlib.sha256(codec.encoded(hashes)).hexdigest()}


def personal_semantics(root, base, head, run_id, scratch, entries):
    materialize(root, head, 'operations/private', scratch)
    templates = Path(__file__).resolve().parents[1] / 'operations/templates'
    shutil.copytree(templates, scratch / 'operations/templates')
    private = scratch / 'operations/private'
    policy = json.loads((private / 'operation-policy.json').read_text())
    require(policy.get('operation_mode') == 'PAPER' and policy.get('broker_submission') == 'HUMAN_ONLY', 'PAPER/HUMAN_ONLY required')
    before = read_json(root, base, 'operations/private/state.json')
    after = read_json(root, head, 'operations/private/state.json')
    require({k:v for k,v in before.items() if k not in STATE_FIELDS} ==
            {k:v for k,v in after.items() if k not in STATE_FIELDS}, 'non-runtime state changed')
    require(after['state_revision'] == before['state_revision'] + 1, 'state revision must advance once')
    require(after['last_run_id'] == run_id and (before.get('last_run_id') or '') < run_id, 'new run required; retries/history edits need review')
    old_water = read_json(root, base, 'operations/private/source-watermarks.json')
    new_water = read_json(root, head, 'operations/private/source-watermarks.json')
    require({k:v for k,v in old_water.items() if k != 'sources'} ==
            {k:v for k,v in new_water.items() if k != 'sources'}, 'watermark configuration changed')
    require(old_water['sources'].keys() == new_water['sources'].keys(), 'source universe change requires review')
    for name, value in new_water['sources'].items():
        require(set(value) == set(old_water['sources'][name]), 'watermark schema changed')
        old_cutoff = old_water['sources'][name].get('last_successful_cutoff_jst')
        new_cutoff = value.get('last_successful_cutoff_jst')
        if old_cutoff:
            require(new_cutoff and datetime.fromisoformat(new_cutoff) >= datetime.fromisoformat(old_cutoff), 'watermark regressed')
    run = private / 'runs' / run_id
    handoff = json.loads((run / 'handoff.json').read_text())
    require(handoff.get('run_id') == run_id and handoff.get('status') == 'completed', 'completed generated run required')
    # Replay the exact existing transition in an isolated expected-state tree.
    # Do not duplicate its field mapping or silently change queue resolution rules.
    try:
        from scripts.daily_operation import _persist_completed_state
    except ModuleNotFoundError:
        from daily_operation import _persist_completed_state
    expected_root = scratch / 'expected-state'
    expected_path = expected_root / 'operations/private/state.json'
    codec.atomic(expected_path, codec.encoded(before))
    _persist_completed_state(root=expected_root, handoff=handoff)
    require(after == json.loads(expected_path.read_text()), 'post-state differs from canonical completed-run transition')
    old_history = git(root, 'show', f'{base}:operations/private/run-history.csv')
    new_history = git(root, 'show', f'{head}:operations/private/run-history.csv')
    require(new_history.startswith(old_history), 'history rewrite rejected')
    added = list(csv.reader(io.StringIO(new_history[len(old_history):].decode())))
    require(len(added) == 1 and added[0][0] == run_id and added[0][4] == 'completed', 'one completed history event required')
    orders = list(csv.DictReader(io.StringIO((run / 'orders.csv').read_text())))
    validate_proposal_events(git(root, 'show', f'{base}:operations/private/trade-event-ledger.csv'),
                            git(root, 'show', f'{head}:operations/private/trade-event-ledger.csv'), orders, run_id)
    try:
        from scripts.operation_state import validate_workspace
        from scripts.operation_policy import validate_policy
        from scripts.run_integrity import validate_run_artifacts
    except ModuleNotFoundError:
        from operation_state import validate_workspace
        from operation_policy import validate_policy
        from run_integrity import validate_run_artifacts
    require(not validate_policy(policy) and not validate_workspace(scratch), 'private schema/policy validation failed')
    validate_run_artifacts(root=scratch, run_id=run_id, completed_at=handoff['completed_at_jst'],
                           source_cutoff=handoff['source_cutoff_jst'], price_date=handoff['price_date'])
    mrs = validate_market_regime_append(root, base, head, run_id, scratch, entries, handoff)
    return {'scope': 'completed_paper_results', 'broker_execution_authorized': False, 'market_regime_evidence': mrs}


def validate_local(root, kind, run_id, base, head):
    root = Path(root).resolve()
    require(Path(git(root, 'rev-parse', '--show-toplevel').decode().strip()).resolve() == root,
            '--root must be the stock.jp.private repository root')
    entries = changes(root, base, head)
    classify(kind, run_id, entries)
    with tempfile.TemporaryDirectory(prefix='daily-publication-') as tmp:
        scratch = Path(tmp)
        details = market_semantics(root, head, scratch, entries) if kind == 'market' else personal_semantics(root, base, head, run_id, scratch, entries)
    return {'validated': True, 'repository': REPOS[kind], 'kind': kind, 'run_id': run_id,
            'base_sha': base, 'head_sha': head, 'paths': sorted(x['path'] for x in entries),
            'validated_at': datetime.now(timezone.utc).isoformat(), **details}


def allow_auto_merge(proof, evidence, now=None):
    """Evidence must be freshly fetched via authorized GitHub read tools, not PR text."""
    now = now or datetime.now(timezone.utc)
    for item, key, age in ((proof, 'validated_at', 1800), (evidence, 'fetched_at', 60)):
        elapsed = (now - datetime.fromisoformat(item[key])).total_seconds()
        require(0 <= elapsed <= age, 'stale validation or GitHub snapshot')
    kind = proof['kind']; pr = evidence['pr']
    require(proof.get('history_status') in (None, 'COMPLETE', 'BATCH_EXHAUSTED'),
            'unfinished or failed history needs review; no automatic merge')
    require(proof.get('validated') is True and proof['repository'] == REPOS[kind], 'invalid semantic proof')
    require(evidence['repository'] == REPOS[kind] and evidence['visibility'] == 'private', 'private destination required')
    require(evidence.get('auto_merge_allowed') is True, 'native auto-merge unavailable; do not alter settings')
    require(pr['state'] == 'open' and pr['draft'] is False and pr.get('merged') is False, 'open non-draft daily PR required')
    require(pr['base_ref'] == 'main' and pr['head_repository'] == REPOS[kind] and pr['author_login'] == 'kkyosuke', 'unexpected PR identity')
    require(pr['head_ref'] == f"daily/{kind}/{proof['run_id']}", 'daily branch identity required')
    require(pr['head_sha'] == proof['head_sha'] and pr['base_sha'] == proof['base_sha'] == evidence['main_sha'], 'head/base moved; revalidate')
    require(evidence.get('reviews_complete') is True and 'review_decision' in pr, 'review evidence incomplete')
    require(pr.get('mergeable') is True and pr['review_decision'] in {None, 'APPROVED', 'REVIEW_REQUIRED'}, 'conflict or requested changes')
    require(evidence.get('checks_complete') is True and evidence.get('files_complete') is True, 'pagination must be complete')
    require(sorted(evidence['changed_paths']) == proof['paths'], 'remote diff differs from validated commit')
    require(evidence.get('statuses_complete') is True and evidence.get('runs_complete') is True, 'all status/run pages required')
    require(all(x['sha'] == proof['head_sha'] and x['state'] == 'success' for x in evidence['statuses']), 'pending/failed legacy status')
    runs = evidence['workflow_runs']
    require(all(x['head_sha'] == proof['head_sha'] and x['status'] == 'completed' and x['conclusion'] == 'success'
                and x['event'] == 'pull_request' for x in runs), 'workflow still pending or failed')
    require(WORKFLOWS[kind] <= {x['path'] for x in runs}, 'expected workflow missing')
    checks = evidence['checks']
    require(bool(checks), 'no CI results')
    require(all(c['head_sha'] == proof['head_sha'] and c['status'] == 'completed' and c['conclusion'] == 'success'
                and c['app_slug'] == 'github-actions' for c in checks), 'wait for all exact-head checks to succeed')
    names = [c['name'] for c in checks]
    require(len(names) == len(set(names)) and CHECKS[kind] <= set(names), 'expected trusted checks missing or duplicated')
    return {'eligible': True, 'action': 'enable_native_auto_merge', 'repository': REPOS[kind],
            'pr_number': pr['number'], 'head_sha': proof['head_sha'], 'base_sha': proof['base_sha']}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    v = sub.add_parser('validate')
    v.add_argument('--root', type=Path, required=True); v.add_argument('--kind', choices=REPOS, required=True)
    v.add_argument('--run-id', required=True); v.add_argument('--base', required=True); v.add_argument('--head', required=True)
    v.add_argument('--output', type=Path, required=True)
    g = sub.add_parser('gate'); g.add_argument('--root', type=Path, required=True); g.add_argument('--proof', type=Path, required=True); g.add_argument('--evidence', type=Path, required=True)
    args = p.parse_args()
    try:
        if args.command == 'validate':
            result = validate_local(args.root, args.kind, args.run_id, args.base, args.head)
            codec.atomic(args.output, codec.encoded(result))
            print(json.dumps({'validated': True, 'scope': result['scope'], 'head_sha': result['head_sha']}))
        else:
            proof = json.loads(args.proof.read_text())
            fresh = validate_local(args.root, proof['kind'], proof['run_id'], proof['base_sha'], proof['head_sha'])
            require({k:v for k,v in proof.items() if k != 'validated_at'} ==
                    {k:v for k,v in fresh.items() if k != 'validated_at'}, 'local proof changed')
            print(json.dumps(allow_auto_merge(proof, json.loads(args.evidence.read_text()))))
        return 0
    except Exception:
        print(json.dumps({'eligible': False, 'reason': 'daily publication validation failed; manual review or fresh evidence required'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
