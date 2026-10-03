"""Default-off GitHub checkpoint adapter; transport is explicitly injected.

No CLI, credential lookup, HTTP client, bootstrap, PR, or workflow dispatch.
Only a separately authorized, existing checkpoint branch may be appended.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

try:
    from scripts import market_data_git as codec, jquants_ingest as ingest
except ModuleNotFoundError:
    import market_data_git as codec, jquants_ingest as ingest

REPO = 'kkyosuke/stock.jp.private'
REF = 'heads/automation/daily-checkpoint-v1'
LIMIT = 20 * 1024 * 1024  # conservative JSON/blob limit; overflow stops before upload


class RemoteStop(RuntimeError):
    """Sanitized fail-closed error; do not retry a write with unknown outcome."""


def require(ok, message):
    if not ok:
        raise RemoteStop(message)


def sha(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value), 'invalid Git identity')
    return value


def git_blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def gate(facts):
    require(isinstance(facts, dict), 'activation evidence missing')
    for key in ('enabled', 'publication_authorized', 'checkpoint_authorized',
                'old_scheduler_stopped', 'main_restored_and_verified', 'exclusive_writer'):
        require(facts.get(key) is True, 'activation evidence missing: ' + key)
    for key in ('publication_rejected', 'unresolved_history', 'open_daily_pr'):
        require(facts.get(key) is False, 'unresolved blocker: ' + key)
    require(facts.get('repository') == REPO and facts.get('visibility') == 'private', 'repository mismatch')
    require((facts.get('mode'), facts.get('submission'), facts.get('plan')) ==
            ('PAPER', 'HUMAN_ONLY', 'light'), 'execution boundary mismatch')


def validate(document):
    require(isinstance(document, dict) and set(document) ==
            {'version', 'writer', 'blocked_campaigns', 'runs', 'snapshot'}, 'invalid checkpoint schema')
    require(document['version'] == 1 and isinstance(document['writer'], str)
            and re.fullmatch('[A-Za-z0-9_-]{1,80}', document['writer']), 'invalid writer')
    require(document['blocked_campaigns'] == [], 'blocked campaign; no migration or publication')
    require(isinstance(document['runs'], dict), 'invalid global daily ledger')
    # Import lazily to avoid a daily-runner module cycle.
    try:
        from scripts import jquants_daily as daily
    except ModuleNotFoundError:
        import jquants_daily as daily
    for day, run in document['runs'].items():
        require(isinstance(run, dict) and set(run) == {'state', 'phase', 'retry_not_before'}, 'invalid remote run')
        daily.validate_state(run['state'])
        require(day == run['state']['run_date'], 'run date mismatch')
        require(run['phase'] in {'IDLE', 'IN_FLIGHT', 'PAUSED', 'BLOCKED', 'COLLECTED', 'RESTORED'}, 'invalid phase')
        if run['retry_not_before'] is not None:
            daily.aware(run['retry_not_before'])
    require(document['snapshot'] is None or isinstance(document['snapshot'], dict), 'invalid snapshot')
    codec.scan(codec.encoded(document))


class GitCheckpointStore:
    """Git Data REST protocol. request(method, relative_path, body) -> JSON.

    The caller supplies authenticated transport with no redirects or automatic
    write retries. This class intentionally cannot discover credentials/connect.
    A fixed existing ref prevents accidental main/history-branch writes.
    """
    def __init__(self, request, *, authorization=None):
        self.request, self.authorization = request, authorization or {}
        self.prefix = '/repos/' + REPO

    def call(self, method, suffix, body=None):
        gate(self.authorization)
        try:
            return self.request(method, self.prefix + suffix, body)
        except Exception:
            raise RemoteStop('GitHub checkpoint transport failed; outcome may be unknown') from None

    def read(self):
        repo = self.call('GET', '')
        require(repo.get('full_name') == REPO and repo.get('private') is True, 'repository is not expected private repository')
        revision = sha(self.call('GET', '/git/ref/' + REF)['object']['sha'])
        commit = self.call('GET', '/git/commits/' + revision)
        require(commit.get('sha') == revision, 'commit identity mismatch')
        tree = self.call('GET', '/git/trees/' + sha(commit['tree']['sha']))
        require(tree.get('truncated') is False and len(tree.get('tree', [])) == 1, 'unexpected checkpoint tree')
        entry = tree['tree'][0]
        require((entry.get('path'), entry.get('mode'), entry.get('type')) ==
                ('checkpoint.json', '100644', 'blob'), 'unexpected checkpoint member')
        blob_sha = sha(entry['sha'])
        blob = self.call('GET', '/git/blobs/' + blob_sha)
        require(blob.get('encoding') == 'base64' and type(blob.get('size')) is int
                and 0 <= blob['size'] <= LIMIT, 'invalid checkpoint blob')
        try:
            raw = base64.b64decode(''.join(blob['content'].split()), validate=True)
            require(len(raw) == blob['size'] and git_blob(raw) == blob_sha, 'checkpoint blob hash mismatch')
            value = json.loads(raw)
            validate(value)
        except (ValueError, KeyError, TypeError):
            raise RemoteStop('invalid remote checkpoint') from None
        return revision, value

    def cas(self, expected, value):
        sha(expected)
        validate(value)
        raw = codec.encoded(value)
        require(len(raw) <= LIMIT, 'checkpoint size exceeds reviewed limit')
        current, previous = self.read()
        require(current == expected and previous['writer'] == value['writer'], 'stale writer or parent')
        for day, old in previous['runs'].items():
            require(day in value['runs'], 'daily ledger deletion forbidden')
            before, after = old['state'], value['runs'][day]['state']
            require(after['budget'] == before['budget'] and after['config_sha256'] == before['config_sha256']
                    and after['requests'][:len(before['requests'])] == before['requests'],
                    'daily budget or reservation history rewrite forbidden')
        # No base_tree: this isolated branch contains only checkpoint.json.
        blob = self.call('POST', '/git/blobs', {'content': base64.b64encode(raw).decode(), 'encoding': 'base64'})
        require(blob.get('sha') == git_blob(raw), 'blob acknowledgement mismatch')
        tree = self.call('POST', '/git/trees', {'tree': [dict(path='checkpoint.json', mode='100644', type='blob', sha=blob['sha'])]})
        commit = self.call('POST', '/git/commits', {'message': 'Daily checkpoint', 'tree': sha(tree['sha']), 'parents': [expected]})
        revision = sha(commit['sha'])
        ack = self.call('PATCH', '/git/refs/' + REF, {'sha': revision, 'force': False})
        require(ack.get('object', {}).get('sha') == revision, 'ref acknowledgement mismatch')
        actual, restored = self.read()
        require(actual == revision and restored == value, 'remote checkpoint readback mismatch')
        return revision


def reject_history(source):
    require(not (source / 'jquants/history').exists() and not (source / 'jquants/history-marker.json').exists(),
            'history payload forbidden in daily Actions adapter')


def capture(source):
    """Use the adopted codec on an isolated copy; caller holds source locks."""
    reject_history(source)
    codec.safe_tree(source)
    with tempfile.TemporaryDirectory() as temporary:
        copied, store = Path(temporary) / 'source', Path(temporary) / 'store'
        copied.mkdir()
        for path in source.rglob('*'):
            if not path.is_file() or path.name == '.ingestion.lock':
                continue
            name = path.relative_to(source).as_posix()
            codec.safe_name(name)
            codec.atomic(copied / name, path.read_bytes())
        codec.pack(copied, store)
        return {p.relative_to(store).as_posix(): base64.b64encode(p.read_bytes()).decode()
                for p in store.rglob('*') if p.is_file()}


def unpack(snapshot, destination):
    require(isinstance(snapshot, dict) and 'current.json' in snapshot, 'snapshot missing')
    with tempfile.TemporaryDirectory() as temporary:
        store = Path(temporary) / 'store'
        store.mkdir()
        for name, value in snapshot.items():
            require(isinstance(name, str) and (name == 'current.json' or re.fullmatch(
                r'(objects|generations)/[0-9a-f]{2}/[0-9a-f]{64}\.gz', name)), 'unsafe codec member')
            try:
                raw = base64.b64decode(value, validate=True)
            except (ValueError, TypeError):
                raise RemoteStop('invalid codec encoding') from None
            codec.atomic(store / name, raw)
        codec.restore(store, destination)
        reject_history(destination)


class RunnerCheckpoint:
    """Synchronous hooks for the existing daily runner, default disabled.

    The trusted caller must verify evidence; booleans are not a permission grant.
    No bootstrap or terminal-to-next-day adoption transition is provided here.
    """
    def __init__(self, store, *, writer, authorization=None, clock=None):
        self.store, self.writer = store, writer
        self.authorization = authorization or {}
        self.clock = clock or (lambda: datetime.now(ZoneInfo('Asia/Tokyo')))
        self.poisoned = False
        self.document = self.revision = self.day = self.source = None

    def commit(self):
        require(not self.poisoned, 'checkpoint failure requires fresh verified recovery')
        try:
            validate(self.document)
            revision = self.store.cas(self.revision, copy.deepcopy(self.document))
            actual, restored = self.store.read()
            require(actual == revision and restored == self.document, 'checkpoint ACK not verified')
            self.revision = revision
        except BaseException:
            self.poisoned = True
            raise

    def prepare(self, source):
        gate(self.authorization)
        self.source = Path(source)
        reject_history(self.source)  # before even read-only GitHub communication
        self.revision, self.document = self.store.read()
        validate(self.document)
        require(self.document['writer'] == self.writer, 'writer ownership mismatch')
        require(all(r['phase'] != 'IN_FLIGHT' for r in self.document['runs'].values()),
                'ambiguous reservation; no automatic resend/refund')
        snapshot = self.document['snapshot']
        if snapshot is not None:
            with tempfile.TemporaryDirectory() as temporary:
                restored = Path(temporary) / 'restored'
                unpack(snapshot, restored)
                if self.source.exists() and any(p.is_file() and p.name != '.ingestion.lock' for p in self.source.rglob('*')):
                    require(capture(self.source) == snapshot, 'local/remote checkpoint divergence')
                else:
                    for path in restored.rglob('*'):
                        if path.is_file():
                            codec.atomic(self.source / path.relative_to(restored), path.read_bytes())
        else:
            require(not self.document['runs'], 'missing snapshot; budget must not reset')

    def attach(self, state):
        self.day = state['run_date']
        runs = self.document['runs']
        require(all(day <= self.day for day in runs), 'out-of-order daily run')
        require(all(day == self.day or run['phase'] == 'RESTORED' for day, run in runs.items()),
                'prior run not adopted and restored')
        if self.day in runs:
            require(runs[self.day]['state'] == state, 'journal mismatch; do not reset budget')
        else:
            require(not state['requests'], 'cannot import existing campaign or consumed journal')
            runs[self.day] = {'state': copy.deepcopy(state), 'phase': 'IDLE', 'retry_not_before': None}
            self.sync(state)

    def sync(self, state):
        require(not self.poisoned, 'checkpoint failure requires fresh verified recovery')
        run = self.document['runs'][self.day]
        if run['phase'] == 'IN_FLIGHT':
            return  # do not claim a finally/local save resolved an unknown response
        require(state['requests'] == run['state']['requests'], 'reservation ledger changed outside boundary')
        run['state'] = copy.deepcopy(state)
        if state['status'] in {'COLLECTED', 'HOLIDAY'}:
            run['phase'] = 'COLLECTED'  # publication and restore still required
        self.document['snapshot'] = capture(self.source)
        self.commit()

    def reserve(self, state):
        require(not self.poisoned, 'checkpoint failure requires fresh verified recovery')
        run = self.document['runs'][self.day]
        now = self.clock()
        require(now.tzinfo is not None and now.astimezone(ZoneInfo('Asia/Tokyo')).date().isoformat() == self.day, 'JST day changed')
        require(run['phase'] in {'IDLE', 'PAUSED'}, 'ambiguous or blocked run')
        if run['retry_not_before']:
            require(now >= datetime.fromisoformat(run['retry_not_before']), 'retry cooldown pending')
        previous = run['state']['requests']
        require(state['requests'][:-1] == previous and len(state['requests']) == len(previous) + 1
                and len(state['requests']) <= state['budget'] <= 12, 'persistent daily budget exhausted or changed')
        require(state['config_sha256'] == run['state']['config_sha256'] and state['budget'] == run['state']['budget'], 'run configuration changed')
        run['state'] = copy.deepcopy(state)
        run['phase'] = 'IN_FLIGHT'
        self.document['snapshot'] = capture(self.source)
        self.commit()  # no financial API until CAS and readback both succeed

    def page_saved(self, state):
        run = self.document['runs'][self.day]
        require(run['phase'] == 'IN_FLIGHT', 'no response reservation')
        # Includes the immutable page AND pagination checkpoint, not just payload.
        self.document['snapshot'] = capture(self.source)
        run['state'] = copy.deepcopy(state)
        run['phase'], run['retry_not_before'] = 'IDLE', None
        self.commit()

    def failed(self, status):
        run = self.document['runs'][self.day]
        require(run['phase'] == 'IN_FLIGHT', 'no error reservation')
        if status not in {401, 403, 429, 500, 502, 503, 504}:
            return  # unknown I/O or invalid response remains charged IN_FLIGHT
        run['phase'] = 'BLOCKED' if status in {401, 403} else 'PAUSED'
        run['retry_not_before'] = (self.clock() + timedelta(seconds=120)).isoformat() if status == 429 else None
        self.commit()
