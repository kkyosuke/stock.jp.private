"""Fixed operator retirement evidence and live GitHub-only run ownership.

This verifies hashes, owner approval and GitHub run attempts. It cannot query
ChatGPT task state or prevent an external process using an old API key.
"""
import base64
from datetime import datetime
import json
import re
import stock_jp.automation.daily_remote as r
import stock_jp.automation.publication as pub
import stock_jp.market_data.storage as codec

RETIREMENT = 'operations/evidence/automation-retirement.json'
WORKFLOW = '.github/workflows/market-data-manual.yml'
MANUAL_ACTOR = 'kkyosuke'


def manual_context(context):
    r.require(context.get('event') == 'workflow_dispatch'
              and context.get('actor') == MANUAL_ACTOR
              and context.get('triggering_actor') == MANUAL_ACTOR,
              'manual dispatch requires repository owner as actor and triggering actor')


def identity(context, scope):
    value = dict(run_id=context.get('run_id'), run_attempt=context.get('run_attempt'),
                 head_sha=context.get('sha'), activation_id=scope.get('activation_id'))
    r.require(all(type(value[k]) is int and value[k] > 0 for k in ('run_id', 'run_attempt')),
              'GitHub run identity missing')
    r.sha(value['head_sha'])
    r.require(isinstance(value['activation_id'], str) and
              re.fullmatch('[A-Za-z0-9_-]{1,80}', value['activation_id']), 'activation identity missing')
    return value


def validate_identity(value):
    r.require(isinstance(value, dict) and set(value) == {'run_id', 'run_attempt', 'head_sha', 'activation_id'},
              'invalid persisted owner identity')
    identity(dict(run_id=value['run_id'], run_attempt=value['run_attempt'], sha=value['head_sha']),
             dict(activation_id=value['activation_id']))


def validate_ledger(document):
    r.require('targets' in document and 'run_owner' in document and 'owner_history' in document,
              'owner and target ledger must be bound together')
    validate_identity(document['run_owner'])
    audit = document['owner_history']
    r.require(isinstance(audit, list) and bool(audit), 'owner history missing')
    previous = None
    for entry in audit:
        r.require(set(entry) == {'previous', 'current', 'observed_at'} and entry['previous'] == previous,
                  'owner transfer chain mismatch')
        validate_identity(entry['current'])
        r.require(entry['current']['activation_id'] == document['run_owner']['activation_id'],
                  'owner activation changed')
        r.require(datetime.fromisoformat(entry['observed_at']).tzinfo is not None, 'owner observation timestamp missing')
        previous = entry['current']
    r.require(previous == document['run_owner'], 'current owner differs from transfer chain')


def run(request, owner, *, active):
    path = pub.PREFIX + '/actions/runs/' + str(owner['run_id']) + '/attempts/' + str(owner['run_attempt'])
    value = request('GET', path, None)
    r.require(value.get('id') == owner['run_id'] and value.get('run_attempt') == owner['run_attempt']
              and value.get('head_sha') == owner['head_sha'] and value.get('head_branch') == 'main'
              and value.get('path') == WORKFLOW and value.get('event') == 'workflow_dispatch'
              and value.get('repository', {}).get('full_name') == r.REPO
              and value.get('actor', {}).get('login') == MANUAL_ACTOR
              and value.get('triggering_actor', {}).get('login') == MANUAL_ACTOR,
              'GitHub run attempt identity mismatch')
    r.require(value.get('status') == ('in_progress' if active else 'completed'),
              'current owner not running' if active else 'previous owner termination unconfirmed; TTL is insufficient')
    return value


def retirement(request, scope, pin, main, now):
    r.require(scope.get('version') == 2 and scope.get('assurance') == 'retired-external-cooperative-github',
              'retirement assurance scope missing')
    r.sha(scope.get('checkpoint_initial_sha'))
    number = scope.get('approval_pr')
    r.require(type(number) is int and number > 0, 'owner approval PR missing')
    pr = request('GET', pub.PREFIX + '/pulls/' + str(number), None)
    r.require(pr.get('merged') is True and pr['base']['ref'] == 'main'
              and pr['base']['repo']['full_name'] == r.REPO, 'owner approval not adopted')
    statement = 'APPROVE_STOCK_AUTOMATION sha256=' + pin
    r.require(any(c['user']['login'] == 'kkyosuke' and c['body'] == statement
                  for c in pub.pages(request, '/issues/' + str(number) + '/comments')),
              'independent owner approval statement missing')
    return retirement_receipt(request, scope, main, now)


def retirement_receipt(request, scope, main, now):
    """Shared receipt validation; callers verify their own pinned owner approval."""
    item = request('GET', pub.PREFIX + '/contents/' + RETIREMENT + '?ref=' + main, None)
    r.require(item.get('type') == 'file' and item.get('encoding') == 'base64', 'retirement receipt missing')
    raw = base64.b64decode(''.join(item['content'].split()), validate=True)
    r.require(codec.digest(raw) == scope.get('retirement_sha256'), 'retirement receipt hash mismatch')
    receipt = json.loads(raw)
    r.require(set(receipt) == {'version', 'repository', 'activation_id', 'observed_by', 'observed_at',
                              'scheduler', 'cloud_writers', 'evidence'}, 'invalid retirement schema')
    r.require(receipt['version'] == 1 and receipt['repository'] == r.REPO
              and receipt['activation_id'] == scope['activation_id'] and receipt['observed_by'] == 'kkyosuke',
              'retirement identity mismatch')
    observed = datetime.fromisoformat(receipt['observed_at'])
    r.require(observed.tzinfo is not None and observed <= now, 'invalid retirement observation time')
    scheduler = receipt['scheduler']
    r.require(set(scheduler) == {'id', 'enabled'} and isinstance(scheduler['id'], str) and scheduler['id']
              and scheduler['enabled'] is False, 'scheduler retirement unconfirmed')
    writers = receipt['cloud_writers']
    r.require(isinstance(writers, list) and bool(writers), 'cloud writer inventory missing')
    for writer in writers:
        r.require(set(writer) == {'id', 'terminated', 'queued_resume'} and isinstance(writer['id'], str)
                  and writer['id'] and writer['terminated'] is True and writer['queued_resume'] is False,
                  'cloud writer retirement unconfirmed')
    r.require(len({w['id'] for w in writers}) == len(writers), 'duplicate retired writer')
    evidence = receipt['evidence']
    r.require(isinstance(evidence, list) and bool(evidence), 'retirement supporting evidence missing')
    for item in evidence:
        r.require(set(item) == {'reference', 'sha256'} and isinstance(item['reference'], str)
                  and item['reference'] and isinstance(item['sha256'], str)
                  and re.fullmatch('[0-9a-f]{64}', item['sha256']), 'invalid retirement evidence reference')
    # References describe owner-reviewed evidence; we do not invent an API to
    # independently inspect the external scheduler or its terminated processes.
    return receipt


def ancestry(request, initial, head):
    comparison = request('GET', pub.PREFIX + '/compare/' + r.sha(initial) + '...' + r.sha(head), None)
    r.require(comparison.get('status') in {'identical', 'ahead'}
              and comparison.get('merge_base_commit', {}).get('sha') == initial,
              'checkpoint no longer descends from approved initial SHA')


def healthy(document):
    r.require(not document['blocked_campaigns'], 'rejected campaign retained; activation blocked')
    r.require(not document.get('publication_intent'), 'unresolved publication intent requires review')
    envelopes = list(document['runs'].values()) + ([document['history']] if document.get('history') else [])
    r.require(all(v['phase'] not in {'IN_FLIGHT', 'BLOCKED'} for v in envelopes),
              'unknown outcome or blocked campaign retained')
    r.require(not document.get('history') or document['history']['phase'] == 'RESTORED',
              'legacy history campaign unresolved')


def claim(checkpoint, scope, context):
    document = checkpoint.document
    healthy(document)
    current = identity(context, scope)
    old = document.get('run_owner')
    run(checkpoint.store.request, current, active=True)
    if old != current:
        if old:
            r.require(old['activation_id'] == current['activation_id'], 'activation changed; no budget reset')
            run(checkpoint.store.request, old, active=False)
        document.setdefault('owner_history', []).append(dict(previous=old, current=current,
                                                            observed_at=checkpoint.clock().isoformat()))
        document['run_owner'] = current
        checkpoint.commit()
    return current


def observe(checkpoint, owner):
    started = checkpoint.clock()
    run(checkpoint.store.request, owner, active=True)
    revision, document = checkpoint.store.read()
    r.require(revision == checkpoint.revision and document.get('run_owner') == owner,
              'checkpoint owner or revision changed')
    r.gate(checkpoint.authorization, now=checkpoint.clock())
    r.require(0 <= (checkpoint.clock() - started).total_seconds() <= 60, 'GitHub owner observation expired')
    return started
