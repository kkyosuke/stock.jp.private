"""Offline review model for a future Actions collector. No network adapter exists.

The adopted daily runner/codec remain the collection/serialization authorities.
This module specifies the durable boundary they must cross before each request;
passing these synthetic tests is NOT authorization or live adapter readiness.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Protocol

REPOSITORY = 'kkyosuke/stock.jp.private'
MAX_REQUESTS = 12
TERMINAL = {'RESTORED', 'HOLIDAY_RESTORED'}
ENDPOINTS = {'/markets/calendar', '/equities/master', '/equities/bars/daily', '/indices/bars/daily/topix'}
STAGES = ['main_restore_verify', 'remote_reservation', 'collect_one_page', 'remote_checkpoint',
          'pack_semantic_secret_verify', 'data_pr', 'exact_head_ci_and_reviews',
          'sha_guarded_squash', 'remote_main_restore_hash_verify']


class Stopped(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Stopped(reason)


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def preflight(facts):
    """Facts must be independently observed, never populated from PR text."""
    checks = {
        'enabled': 'workflow remains disabled',
        'publication_authorized': 'contract-data publication authorization unresolved',
        'old_scheduler_stopped': 'old scheduler stop unverified',
        'exclusive_writer': 'exclusive writer not established',
        'main_restored_and_verified': 'main restore/hash verification missing',
        'checkpoint_verified': 'remote checkpoint missing or invalid',
        'ci_credential_verified': 'unattended CI credential unverified',
        'live_adapter_reviewed': 'live persistence/runner adapter not implemented or reviewed',
    }
    blockers = [reason for field, reason in checks.items() if facts.get(field) is not True]
    for field in ('unresolved_history', 'publication_rejected', 'open_daily_pr', 'unknown_unsaved_results'):
        if facts.get(field) is not False:
            blockers.append(field)
    if facts.get('repository') != REPOSITORY or facts.get('visibility') != 'private':
        blockers.append('wrong repository or visibility')
    if (facts.get('plan'), facts.get('mode'), facts.get('submission')) != ('light', 'PAPER', 'HUMAN_ONLY'):
        blockers.append('contract or execution boundary mismatch')
    if facts.get('price_coverage_min') != .98 or facts.get('active_coverage_min') != 1.0:
        blockers.append('coverage criteria mismatch')
    return {'eligible': not blockers, 'blockers': blockers}


class JournalStore(Protocol):
    """Future adapter: authenticated remote read and linear compare-and-append.

    cas must durably commit before returning. A stale parent MUST fail (never
    force-update). Lost acknowledgements MUST raise; never assume success.
    """
    def read(self) -> tuple[str, dict]: ...
    def cas(self, expected: str, value: dict) -> str: ...


class DurableBoundary:
    def __init__(self, store: JournalStore):
        self.store = store

    def update(self, revision, value):
        acknowledged = self.store.cas(revision, copy.deepcopy(value))
        actual, restored = self.store.read()
        require(actual == acknowledged and restored == value, 'remote write/readback not confirmed')
        return acknowledged

    def begin(self, day: str, main_sha: str, config_sha: str):
        require(date.fromisoformat(day).isoformat() == day, 'invalid JST run date')
        require(re.fullmatch('[0-9a-f]{40}', main_sha) and re.fullmatch('[0-9a-f]{64}', config_sha), 'invalid input identities')
        rev, state = self.store.read()
        require(set(state) == {'runs'}, 'invalid global checkpoint')
        previous = state['runs'].get(day)
        if previous:
            require(previous['main_sha'] == main_sha and previous['config_sha'] == config_sha, 'same-day inputs changed')
            return previous['status']  # rerun does not allocate another budget
        require(all(r['status'] in TERMINAL for r in state['runs'].values()), 'resume unresolved earlier run')
        require(all(d < day for d in state['runs']), 'out-of-order run date')
        state['runs'][day] = {'main_sha': main_sha, 'config_sha': config_sha, 'budget': MAX_REQUESTS,
                              'status': 'READY', 'requests': [], 'retry_not_before': None}
        self.update(rev, state)
        return 'READY'

    def request(self, day, endpoint, query, at: datetime, send, checkpoint_response):
        require(at.tzinfo is not None, 'aware clock required')
        require(at.astimezone(timezone(timedelta(hours=9))).date().isoformat() == day,
                'JST date changed; review unresolved run without resetting its budget')
        require(endpoint in ENDPOINTS, 'unreviewed endpoint')
        rev, state = self.store.read(); run = state['runs'][day]
        require(run['budget'] == MAX_REQUESTS, 'budget changed')
        require(run['status'] in {'READY', 'PAUSED'}, 'ambiguous or terminal run; no automatic resend')
        require(len(run['requests']) < MAX_REQUESTS, 'persistent daily budget exhausted')
        if run['retry_not_before']:
            require(at >= datetime.fromisoformat(run['retry_not_before']), 'cooldown pending')
        receipt = {'endpoint': endpoint, 'query_sha256': checksum(query), 'reserved_at': at.isoformat(),
                   'status': 'RESERVED', 'checkpoint_sha256': None}
        run['requests'].append(receipt); run['status'] = 'IN_FLIGHT'
        revision = self.update(rev, state)  # mandatory remote ACK before send
        response = send(endpoint, query)  # crashes/errors leave charged IN_FLIGHT
        # Adapter must apply existing codec, secret scan and immutable page
        # validation, persist payload remotely, and verify it before returning.
        page_sha = checkpoint_response(response)
        require(isinstance(page_sha, str) and re.fullmatch('[0-9a-f]{64}', page_sha), 'response checkpoint not confirmed')
        receipt['status'] = 'SAVED'; receipt['checkpoint_sha256'] = page_sha
        run['status'] = 'READY'; run['retry_not_before'] = None
        self.update(revision, state)
        return page_sha

    def reconcile_http_error(self, day, status, at):
        """Only a known response may resolve IN_FLIGHT; ambiguous I/O stays stopped."""
        require(at.tzinfo is not None, 'aware clock required')
        require(type(status) is int and (status in {401,403,429} or 500 <= status <= 599), 'unknown permission/error requires review')
        rev, state = self.store.read(); run = state['runs'][day]
        require(run['status'] == 'IN_FLIGHT' and run['requests'][-1]['status'] == 'RESERVED', 'no reserved request')
        run['requests'][-1]['status'] = 'FAILED'
        run['status'] = 'BLOCKED' if status in {401,403} else 'PAUSED'
        run['retry_not_before'] = (at + timedelta(seconds=120)).isoformat() if status == 429 else None
        self.update(rev, state)


def publication_gate(evidence):
    """Additional workflow orchestration checks, not a replacement for adopted validator."""
    require(evidence.get('semantic_proof_valid') is True, 'semantic/secret proof missing')
    require(evidence.get('repository') == REPOSITORY and evidence.get('visibility') == 'private', 'wrong repository or visibility')
    require(evidence.get('publication_authorized') is True and evidence.get('publication_rejected') is False, 'publication blocked')
    require(evidence.get('head') == evidence.get('verified_head') and re.fullmatch('[0-9a-f]{40}', evidence.get('head','')), 'head moved')
    require(evidence.get('base') == evidence.get('main') and re.fullmatch('[0-9a-f]{40}', evidence.get('main','')), 'base moved')
    require(evidence.get('all_pages_verified') is True, 'incomplete remote evidence')
    checks = evidence.get('checks', [])
    require({'format','test','guardrail','ownership-guardrail'} <= {c['name'] for c in checks}, 'missing CI')
    require(all(c['head']==evidence['head'] and c['app']=='github-actions' and c['status']=='completed' and c['conclusion']=='success' for c in checks), 'CI not successful at exact head')
    require(evidence.get('reviews_satisfied') is True and evidence.get('mergeable') is True, 'review/protection/conflict block')
    require(evidence.get('only_archive_paths') is True, 'mixed data/code/private diff')
    return {'eligible':True,'expected_head_sha':evidence['head'],'method':'squash','force':False}


def restored(store, day, expected_generation, actual_generation, hashes_match, holiday=False):
    require(isinstance(expected_generation, str) and re.fullmatch('[0-9a-f]{64}', expected_generation), 'invalid generation')
    require(expected_generation == actual_generation and hashes_match is True, 'remote-main restoration failed')
    rev, state = store.read(); run=state['runs'][day]
    require(run['status']=='MERGED_PENDING_RESTORE', 'merge must be independently confirmed first')
    run['status']='HOLIDAY_RESTORED' if holiday else 'RESTORED'
    DurableBoundary(store).update(rev,state)


if __name__ == '__main__':
    # Intentionally has no live option, token handling, or real transport.
    print(json.dumps({'enabled':False,'live_adapter_available':False,'planned_cron_utc':'0 12 * * *',
                      'stages':STAGES,'decision':'STOP_DISABLED'}))
