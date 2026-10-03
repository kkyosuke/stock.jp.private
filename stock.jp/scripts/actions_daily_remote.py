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
import uuid
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


def gate(facts, *, now=None):
    require(isinstance(facts, dict), 'activation evidence missing')
    for key in ('enabled', 'publication_authorized', 'checkpoint_authorized',
                'old_scheduler_stopped', 'main_restored_and_verified', 'exclusive_writer'):
        require(facts.get(key) is True, 'activation evidence missing: ' + key)
    for key in ('publication_rejected', 'unresolved_history', 'open_daily_pr'):
        require(facts.get(key) is False, 'unresolved blocker: ' + key)
    require(facts.get('repository') == REPO and facts.get('visibility') == 'private', 'repository mismatch')
    require((facts.get('mode'), facts.get('submission'), facts.get('plan')) ==
            ('PAPER', 'HUMAN_ONLY', 'light'), 'execution boundary mismatch')
    if 'expires_at' in facts:
        expiry=datetime.fromisoformat(facts['expires_at'])
        current=now if now is not None else datetime.now(expiry.tzinfo)
        require(expiry.tzinfo is not None and current.tzinfo is not None and current<expiry,'approval expired')


def validate(document):
    required = {'version', 'writer', 'fence', 'blocked_campaigns', 'runs', 'snapshot'}
    require(isinstance(document, dict) and required <= set(document)
            and set(document) <= required | {'history','publication_intent','transaction','github_budget','approval_binding','adopted_history','github_pause'}, 'invalid checkpoint schema')
    if document.get('publication_intent') is not None:
        intent=document['publication_intent']
        require(set(intent)=={'id','state','method','path','request_sha256','identifiers','response'}
                and intent['state'] in {'PENDING','ACKED'}
                and re.fullmatch('[0-9a-f]{32}',intent['id'])
                and re.fullmatch('[0-9a-f]{64}',intent['request_sha256']), 'invalid publication intent')
    require(document['version'] == 2 and isinstance(document['writer'], str)
            and re.fullmatch('[A-Za-z0-9_-]{1,80}', document['writer']), 'invalid writer')
    require(isinstance(document['fence'], str) and re.fullmatch('[0-9a-f]{32}', document['fence']), 'invalid fence')
    require(document['blocked_campaigns'] == [], 'blocked campaign; no migration or publication')
    require(isinstance(document['runs'], dict), 'invalid global daily ledger')
    # Import lazily to avoid a daily-runner module cycle.
    try:
        from scripts import jquants_daily as daily
    except ModuleNotFoundError:
        import jquants_daily as daily
    for day, run in document['runs'].items():
        require(isinstance(run, dict) and set(run) == {'state', 'phase', 'retry_not_before', 'reservation_ids'}, 'invalid remote run')
        daily.validate_state(run['state'])
        ids = run['reservation_ids']
        require(isinstance(ids, list) and len(ids) == len(run['state']['requests'])
                and all(isinstance(n, str) and re.fullmatch('[0-9a-f]{32}', n) for n in ids)
                and len(set(ids)) == len(ids), 'invalid reservation identities')
        require(day == run['state']['run_date'], 'run date mismatch')
        require(run['phase'] in {'IDLE', 'IN_FLIGHT', 'PAUSED', 'BLOCKED', 'COLLECTED', 'RESTORED'}, 'invalid phase')
        if run['retry_not_before'] is not None:
            daily.aware(run['retry_not_before'])
    if document.get('history') is not None:
        validate_history_envelope(document['history'])
    require(document['snapshot'] is None or isinstance(document['snapshot'], dict), 'invalid snapshot')
    codec.scan(codec.encoded({**document,'snapshot':None}))


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
        except RemoteStop:
            raise
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
        check_transition(previous,value)
        # No base_tree: this isolated branch contains only checkpoint.json.
        blob = self.call('POST', '/git/blobs', {'content': base64.b64encode(raw).decode(), 'encoding': 'base64'})
        require(blob.get('sha') == git_blob(raw), 'blob acknowledgement mismatch')
        tree = self.call('POST', '/git/trees', {'tree': [dict(path='checkpoint.json', mode='100644', type='blob', sha=blob['sha'])]})
        # Git ref PATCH is not expected-old-SHA CAS: same-target updates can
        # both succeed. Even identical bodies must produce different candidate
        # commits, including within the same Git timestamp second. The attempt
        # nonce is stored in the immutable commit alongside the state fence.
        attempt = uuid.uuid4().hex
        commit = self.call('POST', '/git/commits', {
            'message': 'Daily checkpoint fence=' + value['fence'] + ' attempt=' + attempt,
            'tree': sha(tree['sha']), 'parents': [expected]})
        revision = sha(commit['sha'])
        ack = self.call('PATCH', '/git/refs/' + REF, {'sha': revision, 'force': False})
        require(ack.get('object', {}).get('sha') == revision, 'ref acknowledgement mismatch')
        actual, restored = self.read()
        require(actual == revision and restored == value, 'remote checkpoint readback mismatch')
        return revision


def reject_history(source):
    require(not (source / 'jquants/history').exists() and not (source / 'jquants/history-marker.json').exists(),
            'history payload forbidden in daily Actions adapter')


def capture(source, history_check=reject_history):
    """Use the adopted codec on an isolated copy; caller holds source locks."""
    history_check(source)
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


def unpack(snapshot, destination, history_check=reject_history):
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
        history_check(destination)


class RunnerCheckpoint:
    """Synchronous hooks for the existing daily runner, default disabled.

    The trusted caller must verify evidence; booleans are not a permission grant.
    No bootstrap or terminal-to-next-day adoption transition is provided here.
    """
    def __init__(self, store, *, writer, authorization=None, clock=None, history_check=reject_history):
        self.store, self.writer = store, writer
        self.authorization = authorization or {}
        self.history_check = history_check
        self.clock = clock or (lambda: datetime.now(ZoneInfo('Asia/Tokyo')))
        self.poisoned = False
        self.document = self.revision = self.day = self.source = None

    def capture_snapshot(self):
        if hasattr(self.store,'capture'):
            return self.store.capture(self.source,self.history_check)
        return capture(self.source,self.history_check)

    def before_reservation(self):
        if self.authorization.get('handover_check'):self.authorization['handover_check']()
        if hasattr(self.store,'ensure_capacity'):self.store.ensure_capacity(self.source)
        if hasattr(self.store.request,'before_provider'):self.store.request.before_provider()

    def before_provider_io(self):
        if self.authorization.get('handover_check'):self.authorization['handover_check']()
        gate(self.authorization,now=self.clock())
        run=self.document['runs'][self.day]
        require(run['phase']=='IN_FLIGHT','provider requires verified reservation')
        now=self.clock()
        require(now.tzinfo is not None and now.astimezone(ZoneInfo('Asia/Tokyo')).date().isoformat()==self.day,
                'JST day changed after reservation; charged request not sent')

    def restore_snapshot(self,snapshot,target):
        if hasattr(self.store,'restore'):
            return self.store.restore(snapshot,target,self.history_check)
        return unpack(snapshot,target,self.history_check)

    def commit(self):
        require(not self.poisoned, 'checkpoint failure requires fresh verified recovery')
        try:
            self.document['fence'] = uuid.uuid4().hex
            if hasattr(self.store.request,'state'):self.document['github_budget']=self.store.request.state()
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
        self.history_check(self.source)  # before even read-only GitHub communication
        self.revision, self.document = self.store.read()
        validate(self.document)
        if hasattr(self.store.request,'restore'):self.store.request.restore(self.document.get('github_budget'))
        require(self.document['writer'] == self.writer, 'writer ownership mismatch')
        require(not self.document.get('publication_intent'),
                'publication transaction requires adoption/recovery before collection')
        require(all(r['phase'] != 'IN_FLIGHT' for r in self.document['runs'].values()),
                'ambiguous reservation; no automatic resend/refund')
        require(not self.document.get('history') or self.document['history']['phase'] != 'IN_FLIGHT',
                'ambiguous history reservation')
        snapshot = self.document['snapshot']
        if snapshot is not None:
            with tempfile.TemporaryDirectory() as temporary:
                restored = Path(temporary) / 'restored'
                self.restore_snapshot(snapshot,restored)
                if self.source.exists() and any(p.is_file() and p.name != '.ingestion.lock' for p in self.source.rglob('*')):
                    require(self.capture_snapshot() == snapshot, 'local/remote checkpoint divergence')
                else:
                    for path in restored.rglob('*'):
                        if path.is_file():
                            codec.atomic(self.source / path.relative_to(restored), path.read_bytes())
        else:
            require(not self.document['runs'] and not self.document.get('history'), 'missing snapshot; budget must not reset')
        self.source.mkdir(parents=True,exist_ok=True)

    def attach(self, state):
        require(not self.document.get('history') or self.document['history']['phase'] == 'RESTORED',
                'history campaign not adopted and restored')
        self.day = state['run_date']
        runs = self.document['runs']
        require(all(day <= self.day for day in runs), 'out-of-order daily run')
        require(all(day == self.day or run['phase'] == 'RESTORED' for day, run in runs.items()),
                'prior run not adopted and restored')
        if self.day in runs:
            require(runs[self.day]['state'] == state, 'journal mismatch; do not reset budget')
        else:
            require(not state['requests'], 'cannot import existing campaign or consumed journal')
            runs[self.day] = {'state': copy.deepcopy(state), 'phase': 'IDLE', 'retry_not_before': None,
                              'reservation_ids': []}
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
        if hasattr(self.store,'capture') and state['status']=='RUNNING':return
        self.document['snapshot'] = self.capture_snapshot()
        self.commit()

    def reserve(self, state):
        if hasattr(self.store.request,'before_provider'):self.store.request.before_provider()
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
        run['reservation_ids'].append(uuid.uuid4().hex)
        self.document['snapshot'] = self.capture_snapshot()
        self.commit()  # no financial API until CAS and readback both succeed

    def page_saved(self, state):
        run = self.document['runs'][self.day]
        require(run['phase'] == 'IN_FLIGHT', 'no response reservation')
        # Includes the immutable page AND pagination checkpoint, not just payload.
        self.document['snapshot'] = self.capture_snapshot()
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


def history_receipts(state):
    return [receipt for batch in state['batches'] for receipt in batch]


def history_monotonic(old, new):
    for key in ('as_of', 'sessions', 'config_sha256', 'batch_limit'):
        require(new[key] == old[key], 'history identity changed')
    before, after = history_receipts(old), history_receipts(new)
    require(after[:len(before)] == before and len(new['batches']) >= len(old['batches']), 'history budget reset')
    require(new['batches'][:len(old['batches'])-1] == old['batches'][:-1], 'prior batch rewrite')
    require(new['batches'][len(old['batches'])-1][:len(old['batches'][-1])] == old['batches'][-1], 'batch reservation rewrite')


def validate_history_envelope(run):
    require(set(run) == {'state','phase','retry_not_before','reservation_ids'}, 'invalid history envelope')
    state = run['state']
    require(type(state['sessions']) is int and 1 <= state['sessions'] <= 280
            and type(state['batch_limit']) is int and 1 <= state['batch_limit'] <= 12, 'invalid history limits')
    batches = state['batches']
    require(isinstance(batches, list) and 1 <= len(batches) <= 50
            and all(isinstance(b, list) and len(b) <= state['batch_limit'] for b in batches)
            and all(len(b) == state['batch_limit'] for b in batches[:-1]), 'invalid history batches')
    receipts = history_receipts(state)
    require(len(receipts) <= 600 and len(run['reservation_ids']) == len(receipts)
            and len(set(run['reservation_ids'])) == len(receipts)
            and all(re.fullmatch('[0-9a-f]{32}', n) for n in run['reservation_ids']), 'invalid history reservations')
    require(run['phase'] in {'IDLE','IN_FLIGHT','PAUSED','BLOCKED','COLLECTED','RESTORED'}, 'invalid history phase')


class HistoryCheckpoint(RunnerCheckpoint):
    """Only a fresh authorized campaign or this remote journal can be attached."""
    def __init__(self, store, *, campaign, **kwargs):
        self.campaign = dict(campaign)
        super().__init__(store, history_check=self.check_history, **kwargs)

    def before_provider_io(self):
        if self.authorization.get('handover_check'):self.authorization['handover_check']()
        gate(self.authorization,now=self.clock())
        require(self.document['history']['phase']=='IN_FLIGHT','history provider requires verified reservation')
        # Historical as_of is fixed; do not apply the daily JST run-date boundary.

    def check_history(self, source):
        path = Path(source)/'jquants/history/state.json'
        if path.exists():
            state = ingest.read_json(path)
            require(all(state.get(k) == v for k,v in self.campaign.items()), 'history outside approved campaign')

    def prepare(self, source):
        # Never import an existing local history as a new remote campaign.
        super().prepare(source)
        require(all(r['phase'] == 'RESTORED' for r in self.document['runs'].values()), 'daily writer unresolved')
        path = self.source/'jquants/history/state.json'
        if path.exists() and not self.document.get('history'):
            require(not history_receipts(ingest.read_json(path)), 'existing history import forbidden')

    def attach(self, state):
        require(all(state.get(k) == v for k,v in self.campaign.items()), 'history campaign identity mismatch')
        run = self.document.get('history')
        if run:
            require(run['state'] == state, 'history remote journal mismatch')
        else:
            require(not history_receipts(state), 'history budget import forbidden')
            self.document['history'] = {'state':copy.deepcopy(state), 'phase':'IDLE',
                                        'retry_not_before':None, 'reservation_ids':[]}
            self.sync(state)

    def sync(self, state):
        require(not self.poisoned, 'checkpoint failure requires fresh recovery')
        run = self.document['history']
        if run['phase'] == 'IN_FLIGHT':
            return
        history_monotonic(run['state'], state)
        require(history_receipts(state) == history_receipts(run['state']), 'unreserved history request')
        run['state'] = copy.deepcopy(state)
        if state['status'] == 'COMPLETE':
            run['phase'] = 'COLLECTED'
        if hasattr(self.store,'capture') and state['status']=='RUNNING':return
        self.document['snapshot'] = self.capture_snapshot()
        self.commit()

    def reserve(self, state):
        if hasattr(self.store.request,'before_provider'):self.store.request.before_provider()
        run = self.document['history']
        require(run['phase'] in {'IDLE','PAUSED'}, 'history ambiguous or blocked')
        if run['retry_not_before']:
            require(self.clock() >= datetime.fromisoformat(run['retry_not_before']), 'history cooldown pending')
        history_monotonic(run['state'], state)
        require(len(history_receipts(state)) == len(history_receipts(run['state']))+1, 'history reservation increment invalid')
        run['state'] = copy.deepcopy(state)
        run['reservation_ids'].append(uuid.uuid4().hex)
        run['phase'] = 'IN_FLIGHT'
        self.document['snapshot'] = self.capture_snapshot()
        self.commit()

    def page_saved(self, state):
        run = self.document['history']
        require(run['phase'] == 'IN_FLIGHT', 'no history reservation')
        self.document['snapshot'] = self.capture_snapshot()
        run['state'] = copy.deepcopy(state)
        run['phase'], run['retry_not_before'] = 'IDLE', None
        self.commit()

    def failed(self, status):
        run = self.document['history']
        require(run['phase'] == 'IN_FLIGHT', 'no history error reservation')
        if status not in {401,403,429,500,502,503,504}:
            return
        run['phase'] = 'BLOCKED' if status in {401,403} else 'PAUSED'
        run['retry_not_before'] = (self.clock()+timedelta(seconds=120)).isoformat() if status == 429 else None
        self.commit()


def check_transition(previous,value):
    require(previous['writer']==value['writer'], 'writer ownership changed')
    if previous.get('adopted_history'):
        require(value.get('adopted_history')==previous['adopted_history'],'adopted history receipt changed')
    if previous.get('approval_binding'):
        require(value.get('approval_binding')==previous['approval_binding'],'approval binding changed')
    pending=previous.get('publication_intent')
    if pending and pending['state']=='PENDING':
        updated=value.get('publication_intent')
        require(updated is not None and all(updated.get(k)==pending[k]
                for k in ('id','method','path','request_sha256','identifiers')),
                'unresolved publication intent cannot be replaced')
    for day, old in previous['runs'].items():
        require(day in value['runs'], 'daily ledger deletion forbidden')
        before, after = old['state'], value['runs'][day]['state']
        require(after['budget'] == before['budget'] and after['config_sha256'] == before['config_sha256']
                and after['requests'][:len(before['requests'])] == before['requests'],
                'daily budget or reservation history rewrite forbidden')
        require(value['runs'][day]['reservation_ids'][:len(old['reservation_ids'])] == old['reservation_ids'],
                'reservation identity rewrite forbidden')
    if previous.get('history'):
        old, new = previous['history'], value.get('history')
        require(new is not None, 'history ledger deletion forbidden')
        history_monotonic(old['state'], new['state'])
        require(new['reservation_ids'][:len(old['reservation_ids'])] == old['reservation_ids'], 'history reservation rewrite')
    require(value['fence'] != previous['fence'], 'fresh checkpoint fence required')
