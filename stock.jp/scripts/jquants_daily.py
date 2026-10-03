"""Bounded daily Light collection. No Git writes, publication, or PAPER runs."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
from zoneinfo import ZoneInfo

try:
    from scripts import jquants_ingest as ingest
    from scripts.jquants_catalog import BASE_URL, BY_NAME, query_for
    from scripts import market_data_git as codec
    from scripts.jquants_archive_validation import validate_archive
except ModuleNotFoundError:
    import jquants_ingest as ingest
    from jquants_catalog import BASE_URL, BY_NAME, query_for
    import market_data_git as codec
    from jquants_archive_validation import validate_archive

JST = ZoneInfo('Asia/Tokyo')
NAMES = ('trading_calendar', 'equities_master', 'equities_daily', 'equities_valuation',
         'financial_summary', 'earnings_dates', 'earnings_calendar', 'investor_types', 'topix_daily')
class PublicationPending(ingest.IngestionError):
    """Expected nonempty daily input has not been published yet."""


TERMINAL = {'COLLECTED', 'HOLIDAY', 'BUDGET_EXHAUSTED', 'BLOCKED'}


def require(condition, message):
    if not condition:
        raise ingest.IngestionError(message)


def aware(value):
    parsed = datetime.fromisoformat(value)
    require(parsed.tzinfo is not None, 'aware timestamp required')
    return parsed.astimezone(JST)


def run_path(archive, day):
    # Stable scheduled UTC ID for a JST date, not the actual execution time.
    return archive / 'runs' / (day.strftime('%Y%m%d') + 'T120000Z.json')


def validate_state(state):
    require(isinstance(state, dict) and set(state) == {
        'daily_runner_version', 'run_date', 'started_at', 'config_sha256', 'budget',
        'requests', 'tasks', 'status', 'data_date', 'error', 'http_status', 'retry_not_before'}, 'invalid daily run schema')
    require(state['daily_runner_version'] == 1 and type(state['budget']) is int
            and 1 <= state['budget'] <= 12, 'invalid daily budget')
    require(state['http_status'] is None or type(state['http_status']) is int and 0 <= state['http_status'] <= 599, 'invalid HTTP status')
    if state['retry_not_before'] is not None:
        aware(state['retry_not_before'])
    day = date.fromisoformat(state['run_date'])
    require(aware(state['started_at']).date() >= day, 'invalid run timestamp')
    require(re.fullmatch('[0-9a-f]{64}', state['config_sha256']) is not None, 'invalid config digest')
    require(state['status'] in TERMINAL | {'RUNNING', 'PAUSED', 'WAIT_PUBLICATION'}, 'invalid run status')
    require(state['data_date'] in (None, day.isoformat()), 'invalid data date')
    require(state['error'] in (None, 'api_error', 'budget_exhausted', 'validation_error', 'publication_pending'), 'invalid run error')
    require(isinstance(state['requests'], list) and len(state['requests']) <= state['budget'], 'invalid request ledger')
    for item in state['requests']:
        require(isinstance(item, dict) and set(item) == {'endpoint', 'query_sha256', 'reserved_at'}, 'invalid request receipt')
        require(item['endpoint'] in {BY_NAME[n].endpoint for n in NAMES}, 'invalid daily endpoint')
        require(re.fullmatch('[0-9a-f]{64}', item['query_sha256']) is not None, 'invalid query digest')
        aware(item['reserved_at'])
    require(isinstance(state['tasks'], dict), 'invalid task ledger')
    for key, value in state['tasks'].items():
        parts = key.split(':')
        require(len(parts) == 2 and parts[0] in NAMES, 'invalid task identity')
        task_day = date.fromisoformat(parts[1])
        require(task_day <= day and (task_day == day or parts[0] == 'financial_summary'), 'invalid task date')
        require(isinstance(value, dict) and set(value) == {'manifest_sha256', 'rows', 'timing'}, 'invalid task receipt')
        require(re.fullmatch('[0-9a-f]{64}', value['manifest_sha256']) is not None, 'invalid manifest digest')
        require(type(value['rows']) is int and value['rows'] >= 0, 'invalid row count')
        require(value['timing'] in ('observed', 'provisional', 'after_final_window'), 'invalid publication timing')


def read_state(path):
    state = ingest.read_json(path)
    validate_state(state)
    return state


@contextmanager
def workspace_lock(private):
    folder = private / 'market-data-staging'
    require(not folder.is_symlink(), 'unsafe staging directory')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / 'daily-runner.lock'
    require(not path.is_symlink(), 'unsafe runner lock')
    with path.open('a+b') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ingest.IngestionError('another daily runner is active') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def partition(archive, name, day):
    """Validate existing manifests, immutable revisions and every response page."""
    path = archive / 'datasets' / name / day.isoformat() / 'latest.json'
    if not path.exists():
        return None
    m = ingest.read_json(path)
    require(m['dataset'] == name and m['provider'] == 'J-Quants API V2'
            and m['endpoint'] == BASE_URL + BY_NAME[name].endpoint
            and m['query'] == query_for(BY_NAME[name], day)
            and m['period'] == {'from': day.isoformat(), 'to': day.isoformat()}
            and m['status'] == 'complete', 'invalid partition identity')
    sha = ingest.digest(path.read_bytes())
    revision = path.parent / 'revisions' / (sha + '.json')
    require(revision.is_file() and revision.read_bytes() == path.read_bytes(), 'invalid partition revision')
    require(isinstance(m['pages'], list) and bool(m['pages']), 'missing partition pages')
    rows = []
    for index, page in enumerate(m['pages']):
        require(re.fullmatch('objects/[0-9a-f]{2}/[0-9a-f]{64}\\.json', page['path']) is not None,
                'invalid response object path')
        payload = ingest.checked_payload(archive, page)
        require(isinstance(payload.get('data'), list) and all(isinstance(r, dict) for r in payload['data']), 'invalid response rows')
        require(page['row_count'] == len(payload['data']) and page['page_number'] == index + 1, 'invalid response counts')
        require(bool(payload.get('pagination_key')) == (index < len(m['pages']) - 1), 'incomplete response pages')
        aware(page['retrieved_at'])
        rows.extend(payload['data'])
    require(m['row_count'] == len(rows), 'invalid partition row total')
    aware(m['started_at']); aware(m['completed_at'])
    return m, rows, sha


def validate_checkpoint(archive, name, day):
    path = archive / 'datasets' / name / day.isoformat() / 'checkpoint.json'
    if not path.exists():
        return
    c = ingest.read_json(path)
    require(set(c) == {'endpoint', 'query', 'started_at', 'pages', 'next_key', 'seen_keys', 'complete'}
            and c['endpoint'] == BY_NAME[name].endpoint and c['query'] == query_for(BY_NAME[name], day),
            'invalid checkpoint identity')
    aware(c['started_at'])
    require(type(c['complete']) is bool and isinstance(c['pages'], list) and isinstance(c['seen_keys'], list), 'invalid checkpoint schema')
    keys = []
    for index, page in enumerate(c['pages']):
        payload = ingest.checked_payload(archive, page)
        require(isinstance(payload.get('data'), list) and all(isinstance(r, dict) for r in payload['data'])
                and page['row_count'] == len(payload['data']) and page['page_number'] == index + 1, 'invalid checkpoint page')
        key = payload.get('pagination_key')
        require(key is None or isinstance(key, str), 'invalid pagination key')
        if key:
            require(key not in keys, 'pagination cycle')
            keys.append(key)
        require(bool(key) or index == len(c['pages']) - 1, 'page after completed response')
    last = (ingest.checked_payload(archive, c['pages'][-1]).get('pagination_key') or None) if c['pages'] else None
    require(c['seen_keys'] == keys and c['next_key'] == last
            and c['complete'] == (bool(c['pages']) and last is None), 'inconsistent checkpoint')
    return c


def recover_published_checkpoint(archive, name, day):
    """Remove only a fully verified checkpoint already durably published.

    The immutable revision and every page remain untouched. A mismatch is not
    evidence that a checkpoint is obsolete; leave it for normal resume/review.
    Caller holds the ingestion lock.
    """
    checkpoint = validate_checkpoint(archive, name, day)
    if not checkpoint or not checkpoint['complete']:
        return False
    existing = partition(archive, name, day)
    if existing is None:
        return False
    manifest = existing[0]
    if not (manifest['endpoint'] == BASE_URL + checkpoint['endpoint']
            and all(manifest[key] == checkpoint[key] for key in ('query', 'started_at', 'pages'))):
        return False
    path = archive / 'datasets' / name / day.isoformat() / 'checkpoint.json'
    path.unlink()
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


class DurableClient(ingest.Client):
    def __init__(self, state, save, clock, remote=None, **kwargs):
        self.state, self.save, self.clock = state, save, clock
        self.remote = remote
        super().__init__(plan='light', max_requests=state['budget'], retries=0, **kwargs)
        self.requests = len(state['requests'])

    def _request(self, endpoint, params):
        if self.remote is not None:self.remote.before_reservation()
        # Charge before network I/O. An ambiguous crash consumes its reservation.
        require(len(self.state['requests']) < self.state['budget'], 'durable budget exhausted')
        self.state['requests'].append({'endpoint': endpoint, 'query_sha256': ingest.digest(ingest.encoded(params)),
                                       'reserved_at': self.clock().isoformat()})
        self.save()
        if self.remote is not None:
            self.remote.reserve(self.state)
            self.remote.before_provider_io()  # after all paced checkpoint writes, immediately before provider I/O
        try:
            return super()._request(endpoint, params)
        except ingest.ApiError as error:
            if self.remote is not None:
                self.remote.failed(error.status)
            raise


def run(root, *, at=None, run_date=None, transport=None, sleep=None, remote=None):
    root = Path(root).resolve()
    activation=root/'.github/stock-automation-approval.json'
    require(remote is not None or not activation.exists() or ingest.read_json(activation).get('enabled') is not True,
            'Actions writer owns acquisition; legacy runner disabled')
    at = (at or datetime.now(JST))
    require(at.tzinfo is not None, 'aware execution time required')
    at = at.astimezone(JST)
    target = getattr(remote, 'target_day', None)
    day = target or run_date or at.date()
    require(day <= at.date(), 'future run date forbidden')
    config_path = root / 'operations/private/source-config.json'
    config = ingest.read_json(config_path)
    c = config['jquants']; settings = c['ingestion']
    require(c['plan'] == 'light' and c.get('addons') == [] and c['enabled'] is True
            and c['base_url'] == BASE_URL and c['api_key_env'] == 'JQUANTS_API_KEY'
            and settings['enabled'] is True and type(settings['max_requests_per_run']) is int
            and 1 <= settings['max_requests_per_run'] <= 12, 'daily Light contract mismatch')
    require(settings['archive_root'] == 'operations/private/market-data/jquants'
            and config['price_source']['archive_root'] == 'operations/private/market-data/daily-prices', 'unexpected daily storage')
    private = (root / 'operations/private').resolve()
    source = private / 'market-data'
    with workspace_lock(private):
        store = root / 'stock.jp/data/jquants-archive'
        if remote is not None:
            remote.prepare(source)
        if not source.exists() and (store / 'current.json').is_file():
            codec.restore(store, source)  # Never overlays local checkpoints.
        source.mkdir(parents=True, exist_ok=True, mode=0o700)
        codec.safe_tree(source)
        archive = ingest.private_root(root, settings['archive_root'])
        with ingest.archive_lock(archive):
            # Include historical revisions whose pages are no longer in latest.json.
            members = {p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()}
            try:
                validate_archive(members, lambda name: (source / name).read_bytes())
            except (ValueError, KeyError, TypeError, OSError):
                raise ingest.IngestionError('retained revision or page validation failed') from None
            # Validate every local partition/checkpoint before any network operation.
            for p in (archive / 'datasets').glob('*/*/latest.json'):
                partition(archive, p.parent.parent.name, date.fromisoformat(p.parent.name))
            for p in (archive / 'datasets').glob('*/*/checkpoint.json'):
                validate_checkpoint(archive, p.parent.parent.name, date.fromisoformat(p.parent.name))
            for p in (archive / 'datasets').glob('*/*/checkpoint.json'):
                recover_published_checkpoint(archive, p.parent.parent.name, date.fromisoformat(p.parent.name))
            path = run_path(archive, day)
            require(target is not None or run_date is None or path.is_file(), 'explicit run date must resume an existing journal')
            for p in (archive / 'runs').glob('*T120000Z.json'):
                old = read_state(p)
                for key, receipt in old['tasks'].items():
                    name, value = key.split(':')
                    revision = archive / 'datasets' / name / value / 'revisions' / (receipt['manifest_sha256'] + '.json')
                    require(revision.is_file() and ingest.digest(revision.read_bytes()) == receipt['manifest_sha256'],
                            'historical run receipt is missing or corrupt')
                require(p == run_path(archive, date.fromisoformat(old['run_date'])), 'run filename mismatch')
                require(p == path or old['status'] in {'COLLECTED', 'HOLIDAY'},
                        'resume prior unfinished run first; blocked or exhausted runs need operator review')
                require(target is not None or date.fromisoformat(old['run_date']) <= day, 'cannot start a run before an existing later run')
            previous_report = ingest.read_json(archive / 'latest.json', {})
            if previous_report.get('scope') == 'daily_bounded':
                previous_date = date.fromisoformat(previous_report['daily_run']['run_date'])
                require(run_path(archive, previous_date).is_file(), 'daily journal missing; do not reset budget')
            sha = ingest.digest(config_path.read_bytes())
            if path.exists():
                state = read_state(path)
                require(state['config_sha256'] == sha and state['budget'] == settings['max_requests_per_run'], 'run configuration changed')
            else:
                state = {'daily_runner_version': 1, 'run_date': day.isoformat(), 'started_at': at.isoformat(),
                         'config_sha256': sha, 'budget': settings['max_requests_per_run'], 'requests': [],
                         'tasks': {}, 'status': 'RUNNING', 'data_date': None, 'error': None,
                         'http_status': None, 'retry_not_before': None}
            if remote is not None:
                validate_state(state)
                ingest.atomic_json(path, state)
                remote.attach(state)
            def save(*, local_only=False):
                validate_state(state)
                ingest.atomic_json(path, state)
                if remote is not None and not local_only:
                    remote.sync(state)
            def result():
                return {'status': state['status'], 'run_date': state['run_date'], 'data_date': state['data_date'],
                        'requests_used': len(state['requests']), 'budget': state['budget'], 'tasks_completed': len(state['tasks']),
                        'readiness': 'NOT_EVALUATED', 'remote_saved': False, 'error': state['error'],
                        'http_status': state['http_status'], 'retry_not_before': state['retry_not_before'],
                        'last_request_endpoint': state['requests'][-1]['endpoint'] if state['requests'] else None}
            # Task receipts remain immutable even if later runs publish corrections.
            for key, receipt in state['tasks'].items():
                name, value = key.split(':')
                revision = archive / 'datasets' / name / value / 'revisions' / (receipt['manifest_sha256'] + '.json')
                require(revision.is_file() and ingest.digest(revision.read_bytes()) == receipt['manifest_sha256'], 'run receipt is missing or corrupt')
            if state['status'] in TERMINAL:
                return result()
            if state['retry_not_before'] and datetime.now(JST) < aware(state['retry_not_before']):
                return result()
            if at < datetime.combine(day, time(21), JST):
                state['status'] = 'WAIT_PUBLICATION'
                save()
                return result()
            save()
            client = None
            def get_client():
                nonlocal client
                if client is None:
                    kwargs = {'transport': transport}
                    if sleep is not None: kwargs['sleep'] = sleep
                    client = DurableClient(state, lambda: save(local_only=True),
                                           lambda: datetime.now(JST), remote=remote, **kwargs)
                return client
            def fetch(name, task_day, *, final=False):
                key = name + ':' + task_day.isoformat()
                if key in state['tasks']:
                    return partition(archive, name, task_day)
                existing = partition(archive, name, task_day)
                final_after = datetime.combine(task_day + timedelta(days=1), time(0, 30), JST)
                reusable = existing and (not final or aware(existing[0]['started_at']) >= final_after)
                if (archive / 'datasets' / name / task_day.isoformat() / 'checkpoint.json').exists():
                    reusable = False
                if name in ('equities_master', 'equities_daily', 'equities_valuation', 'topix_daily') and existing and not existing[1]:
                    reusable = False
                if not reusable:
                    validate_checkpoint(archive, name, task_day)
                    checkpoint = archive / 'datasets' / name / task_day.isoformat() / 'checkpoint.json'
                    if final and checkpoint.exists():
                        require(aware(ingest.read_json(checkpoint)['started_at']) >= final_after,
                                'provisional checkpoint requires its original run; do not overwrite')
                    ingest.collect_partition(archive, BY_NAME[name], task_day, get_client(),
                                             page_saved=(lambda: remote.page_saved(state)) if remote is not None else None)
                    existing = partition(archive, name, task_day)
                m, rows, digest = existing
                if name in ('equities_master', 'equities_daily', 'equities_valuation', 'topix_daily'):
                    if not rows:
                        raise PublicationPending('daily data not yet available')
                    require(all(r.get('Date') == task_day.isoformat() for r in rows), 'daily data date mismatch')
                timing = 'observed'
                if name == 'financial_summary':
                    require(all(r.get('DiscDate') == task_day.isoformat() for r in rows), 'financial disclosure date mismatch')
                    timing = 'after_final_window' if aware(m['started_at']) >= final_after else 'provisional'
                state['tasks'][key] = {'manifest_sha256': digest, 'rows': len(rows), 'timing': timing}
                save()
                return existing
            try:
                state['status'] = 'RUNNING'; state['error'] = None
                state['http_status'] = None; state['retry_not_before'] = None
                if target is not None and remote.profile == 'history':
                    remote.collect_history(state, archive, get_client, fetch, save)
                    if state['status'] == 'COLLECTED': ingest.publish_snapshot(root, archive, day)
                else:
                    # Complete prior provisional financial dates before consuming today's budget.
                    for p in sorted((archive / 'datasets/financial_summary').glob('*/latest.json')):
                        task_day = date.fromisoformat(p.parent.name)
                        if task_day < day and at >= datetime.combine(task_day + timedelta(days=1), time(0, 30), JST):
                            m = partition(archive, 'financial_summary', task_day)[0]
                            if aware(m['started_at']) < datetime.combine(task_day + timedelta(days=1), time(0, 30), JST):
                                fetch('financial_summary', task_day, final=True)
                    calendar = fetch('trading_calendar', day)[1]
                    require(len(calendar) == 1 and calendar[0].get('Date') == day.isoformat()
                            and str(calendar[0].get('HolDiv')) in ('0', '1', '2', '3'), 'invalid trading calendar')
                    if str(calendar[0]['HolDiv']) in ('0', '3'):
                        state['status'] = 'HOLIDAY'
                    else:
                        state['data_date'] = day.isoformat()
                        for name in NAMES[1:]:
                            fetch(name, day)
                        ingest.publish_snapshot(root, archive, day)
                        state['status'] = 'COLLECTED'
            except PublicationPending:
                state['status'], state['error'] = 'PAUSED', 'publication_pending'
            except ingest.RequestBudgetExhausted:
                state['status'], state['error'] = 'BUDGET_EXHAUSTED', 'budget_exhausted'
            except ingest.ApiError as error:
                state['status'] = 'BLOCKED' if error.status in (401, 403) else 'PAUSED'
                state['error'] = 'api_error'
                state['http_status'] = error.status
                if error.status == 429:
                    state['retry_not_before'] = (datetime.now(JST) + timedelta(seconds=120)).isoformat()
            except (ingest.IngestionError, ValueError, KeyError, TypeError, OSError):
                state['status'], state['error'] = 'BLOCKED', 'validation_error'
            finally:
                save()
                # Failures preserve a truthful partial archive, never full history completeness.
                report = ingest.planned_status('light', (), day)
                report.update(scope='daily_bounded', daily_run=result(), all_entitled_complete=False)
                if target is not None:
                    report.update(scope='target_date_bounded', target_date=day.isoformat(),
                                  profile=remote.profile, knowledge='retrieved_now_not_point_in_time',
                                  execution_started_at=state['started_at'])
                ingest.atomic_json(archive / 'latest.json', report)
                if remote is not None:
                    remote.sync(state)
            return result()


def checkout_preflight(root):
    """CLI requires freshly adopted main; library fixtures do not need Git."""
    root = Path(root).resolve()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()
    require(Path(git('rev-parse', '--show-toplevel')) == root, 'repository root required')
    require(git('remote', 'get-url', 'origin') in (
        'https://github.com/kkyosuke/stock.jp.private.git',
        'git@github.com:kkyosuke/stock.jp.private.git'), 'unexpected remote')
    require(git('branch', '--show-current') == 'main' and not git('status', '--porcelain'), 'clean main required')
    remote = git('ls-remote', 'origin', 'refs/heads/main').split()
    require(len(remote) == 2 and remote[0] == git('rev-parse', 'HEAD'), 'latest remote main required')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='single private repository root')
    parser.add_argument('--run-date', type=date.fromisoformat, help='explicit resume of an unfinished JST run date')
    args = parser.parse_args()
    try:
        checkout_preflight(args.root)
        report = run(args.root, run_date=args.run_date)
    except ingest.IngestionError as error:
        report = {'status': 'BLOCKED', 'error': str(error), 'readiness': 'NOT_EVALUATED', 'remote_saved': False}
    except Exception:
        report = {'status': 'BLOCKED', 'error': 'preflight_or_storage_validation', 'readiness': 'NOT_EVALUATED', 'remote_saved': False}
    print(json.dumps(report))
    return 0 if report['status'] in ('COLLECTED', 'HOLIDAY') else 2


if __name__ == '__main__':
    raise SystemExit(main())
