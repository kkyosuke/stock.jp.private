"""Owner-requested local history fill using the existing collector and budget ledger.

This command explicitly advances the fixed campaign's batches. It never creates
an Actions grant, changes an approval, uploads data, or claims analysis readiness.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
from datetime import date
import json
from pathlib import Path

from stock_jp.automation import date_grants, legacy_debit
from stock_jp.market_data.jquants import daily, history, ingest
from stock_jp.workspace import ACQUISITION_LEDGER, MARKET_WORK, project_root


class LocalJournal:
    """Durably charge the shared ledger before each send; retain crash charges.

Implements the history collector's persistence interface locally. The history
state owns request receipts; this record binds their targets to the budget ledger.
"""
    def __init__(self, root: Path, as_of: date, sessions: int):
        self.root = Path(root)
        self.ledger_path = self.root / ACQUISITION_LEDGER
        self.path = self.root / f"operations/acquisition/history-backfill-{as_of}.json"
        ledger = ingest.read_json(self.ledger_path)
        date_grants.validate(ledger)
        campaign = ledger['legacy_debit']['campaign']
        daily.require((campaign['as_of'], campaign['sessions']) == (as_of.isoformat(), sessions),
                      'local fill must use the existing fixed history campaign')
        daily.require(not ledger['grants'], 'pending Actions grants block local acquisition')
        self.record = ingest.read_json(self.path)
        if self.record is None:
            daily.require(not (self.root / MARKET_WORK / 'jquants/history/state.json').exists(),
                          'history exists without its local budget binding; do not reset it')
            self.record = dict(version=1, campaign=copy.deepcopy(campaign),
                               initial_ledger=copy.deepcopy(ledger),
                               initial_ledger_sha256=ingest.digest(self.ledger_path.read_bytes()),
                               history_state='jquants/history/state.json',
                               request_targets=[], requests_sha256=ingest.digest(ingest.encoded([])),
                               history_state_sha256=None, status='READY', last_http_status=None,
                               analysis_readiness='NOT_EVALUATED')
            ingest.atomic_json(self.path, self.record)
        daily.require(self.record['version'] == 1 and self.record['campaign'] == campaign,
                      'local history campaign changed')
        date_grants.validate(self.record['initial_ledger'])
        daily.require(ingest.digest(ingest.encoded(self.record['initial_ledger'])) ==
                      self.record['initial_ledger_sha256'], 'initial local budget binding changed')
        self.state = None

    def prepare(self, source):
        daily.require(Path(source) == self.root / MARKET_WORK and Path(source).is_dir(),
                      'restore the adopted archive before local history collection')

    def attach(self, state):
        self.state = state
        self.sync(state)

    def charged_ledger(self, targets):
        ledger = copy.deepcopy(self.record['initial_ledger'])
        for target, cost in Counter(targets).items():
            ledger['prior_charges'][target] = ledger['prior_charges'].get(target, 0) + cost
            daily.require(ledger['prior_charges'][target] +
                          legacy_debit.charges(ledger['legacy_debit'], target) <= 12,
                          'local target budget exhausted')
        ledger['prior_batches'] += (len(targets) + 11) // 12
        date_grants.validate(ledger)
        return ledger

    def target(self, receipt, state):
        endpoint = receipt['endpoint']
        if endpoint in {history.BY_NAME[n].endpoint for n in ('trading_calendar', 'topix_daily')}:
            value = state['ranges']['trading_calendar' if endpoint == history.BY_NAME['trading_calendar'].endpoint else 'topix_daily']
            queries = [value['query'], *[{**value['query'], 'pagination_key': key} for key in value['seen_keys']]]
            daily.require(any(ingest.digest(ingest.encoded(q)) == receipt['query_sha256'] for q in queries),
                          'shared request query does not match the campaign')
            return self.record['campaign']['start_date']
        dataset = next(n for n in ('equities_master', 'equities_daily')
                       if history.BY_NAME[n].endpoint == endpoint)
        for target in state['days']:
            query = {'date': target}
            checkpoint = ingest.read_json(self.root / MARKET_WORK / 'jquants/datasets' /
                                          dataset / target / 'checkpoint.json', {})
            queries = [query]
            if checkpoint.get('next_key'):
                queries.append({**query, 'pagination_key': checkpoint['next_key']})
            if any(ingest.digest(ingest.encoded(q)) == receipt['query_sha256'] for q in queries):
                return target
        raise ingest.IngestionError('local receipt target cannot be reconciled')

    def sync(self, state):
        daily.require(state['batch_limit'] == 12, 'local fill requires twelve-request batches')
        requests = [r for batch in state['batches'] for r in batch]
        old_targets = self.record['request_targets']
        daily.require(len(requests) >= len(old_targets) and
                      ingest.digest(ingest.encoded(requests[:len(old_targets)])) == self.record['requests_sha256'],
                      'local request history was removed or rewritten')
        targets = old_targets + [self.target(r, state) for r in requests[len(old_targets):]]
        previous, updated = self.charged_ledger(old_targets), self.charged_ledger(targets)
        # Either ordering may survive a crash between the two atomic writes.
        # No third value may silently absorb or discard another writer's debit.
        current = ingest.read_json(self.ledger_path)
        daily.require(current == previous or current == updated, 'shared budget changed during local acquisition')
        if current != updated:
            ingest.atomic_json(self.ledger_path, updated)
        self.record.update(request_targets=targets, requests_sha256=ingest.digest(ingest.encoded(requests)),
                           history_state_sha256=ingest.digest(ingest.encoded(state)), status=state['status'])
        ingest.atomic_json(self.path, self.record)

    def before_reservation(self):
        self.sync(self.state)
        ledger = self.charged_ledger(self.record['request_targets'])
        total = legacy_debit.charges(ledger['legacy_debit']) + sum(ledger['prior_charges'].values())
        if total >= 600:
            raise ingest.RequestBudgetExhausted('shared history budget exhausted')
        # The local command explicitly requests the whole campaign. Advance
        # inside the locked collector so each batch need not parse all prior
        # raw pages again. Reservations still retain the original 12-call bound.
        if len(self.state['batches'][-1]) == self.state['batch_limit'] and len(self.state['batches']) < 50:
            self.state['batches'].append([])
            source = self.root / MARKET_WORK
            history.validate_history(self.state, lambda name: (source / name).read_bytes())
            ingest.atomic_json(source / self.record['history_state'], self.state)
            self.sync(self.state)

    def reserve(self, state):
        self.sync(state)

    def before_provider_io(self):
        daily.require(ingest.read_json(self.ledger_path) == self.charged_ledger(self.record['request_targets']),
                      'request debit was not saved before provider I/O')

    def failed(self, status):
        self.record['last_http_status'] = status
        ingest.atomic_json(self.path, self.record)

    def page_saved(self, state):
        self.sync(state)


def run(root, *, as_of, sessions=280, transport=None, sleep=None, progress=None):
    root = Path(root).resolve()
    journal = LocalJournal(root, as_of, sessions)
    state_path = root / MARKET_WORK / 'jquants/history/state.json'
    state = ingest.read_json(state_path)
    batch = len(state['batches']) if state else 1
    if state and batch < 50 and state['status'] in {'RUNNING', 'PAUSED', 'BATCH_EXHAUSTED'} and \
            len(state['batches'][-1]) == state['batch_limit']:
        batch += 1
    while True:
        result = history.run(root, as_of=as_of, sessions=sessions, batch=batch,
                             remote=journal, transport=transport, sleep=sleep)
        if progress:
            progress(result)
        ledger = ingest.read_json(journal.ledger_path)
        used = legacy_debit.charges(ledger['legacy_debit']) + sum(ledger['prior_charges'].values())
        if result['status'] != 'BATCH_EXHAUSTED' or used >= 600 or batch >= 50:
            return result
        batch += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=project_root())
    parser.add_argument('--as-of', type=date.fromisoformat, required=True)
    parser.add_argument('--sessions', type=int, default=280)
    args = parser.parse_args()
    try:
        result = run(args.root, as_of=args.as_of, sessions=args.sessions,
                     progress=lambda item: print(json.dumps(item), flush=True))
    except (ValueError, OSError, KeyError, TypeError, ingest.IngestionError):
        print(json.dumps({'status': 'BLOCKED', 'error': 'local_history_preflight_or_validation'}))
        return 2
    return 0 if result['status'] == 'COMPLETE' else 2
