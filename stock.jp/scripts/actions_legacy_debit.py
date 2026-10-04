"""Carry reviewed legacy request debits, never legacy data, into target budgets."""
import copy
import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import re
try:
    from scripts import actions_daily_remote as r, jquants_history as history
    from scripts import jquants_ingest as ingest, jquants_daily as daily, market_data_git as codec
    from scripts.jquants_catalog import BY_NAME
except ModuleNotFoundError:
    import actions_daily_remote as r, jquants_history as history
    import jquants_ingest as ingest, jquants_daily as daily, market_data_git as codec
    from jquants_catalog import BY_NAME


def validate(value, campaign):
    r.require(isinstance(value, dict) and set(value) == {
        'version', 'source_commit', 'journal_sha256', 'campaign', 'batches', 'payload_disposition'},
        'invalid legacy debit schema')
    r.sha(value['source_commit'])
    r.require(value['version'] == 1 and value['campaign'] == campaign
              and value['payload_disposition'] == 'REJECTED_NOT_ADOPTED'
              and isinstance(value['journal_sha256'], str)
              and re.fullmatch('[0-9a-f]{64}', value['journal_sha256']), 'legacy debit identity mismatch')
    batches = value['batches']
    r.require(isinstance(batches, list) and 1 <= len(batches) <= 50
              and all(isinstance(b, list) and 1 <= len(b) <= 12 for b in batches)
              and all(len(b) == 12 for b in batches[:-1]), 'invalid legacy debit batches')
    seen = set()
    for batch in batches:
        for entry in batch:
            r.require(isinstance(entry, dict) and set(entry) == {'target_date', 'receipt'},
                      'invalid legacy debit entry')
            day = entry['target_date']
            r.require(isinstance(day, str) and campaign['start_date'] <= day <= campaign['as_of']
                      and date.fromisoformat(day).isoformat() == day, 'legacy debit date mismatch')
            receipt = entry['receipt']
            r.require(isinstance(receipt, dict) and set(receipt) == {'endpoint', 'query_sha256', 'reserved_at'}
                      and receipt['endpoint'] in {BY_NAME[n].endpoint for n in history.NAMES}
                      and isinstance(receipt['query_sha256'], str)
                      and re.fullmatch('[0-9a-f]{64}', receipt['query_sha256']), 'invalid legacy reservation')
            daily.aware(receipt['reserved_at'])
            identity = codec.digest(codec.encoded(receipt))
            r.require(identity not in seen, 'duplicate legacy reservation')
            seen.add(identity)
    codec.scan(codec.encoded(value))


def charges(value, target_date=None):
    return sum(1 for batch in (value or {}).get('batches', []) for entry in batch
               if target_date is None or entry['target_date'] == target_date)


def prepare(source, source_commit):
    """Read-only preparation for inclusion in the existing pinned scope review.

    Every reservation must match a verified durable response, one-for-one. Unknown
    or partially saved requests are rejected, never refunded or called successful.
    Shared requests are conservatively charged to the first campaign date.
    No raw pages, normalized data, task completion or publication state is returned.
    """
    source = Path(source)
    codec.safe_tree(source)
    raw = (source/'jquants/history/state.json').read_bytes()
    state = json.loads(raw)
    read = lambda name: (source/name).read_bytes()
    history.validate_history(state, read)
    r.require(state['status'] == 'BATCH_EXHAUSTED' and state['batch_limit'] == 12
              and state['days'] and not state['retry_not_before'], 'legacy campaign requires outcome reconciliation')
    r.require(not any((source/'jquants/datasets').rglob('checkpoint.json')),
              'partial legacy response requires reconciliation')
    campaign = dict(start_date=state['days'][0], as_of=state['as_of'], sessions=state['sessions'])
    matches = {}
    def pages(name, target_date, query, references):
        token = None
        r.require(bool(references), 'legacy response missing')
        for index, ref in enumerate(references):
            params = dict(query)
            if token: params['pagination_key'] = token
            key = (BY_NAME[name].endpoint, ingest.digest(ingest.encoded(params)))
            r.require(key not in matches, 'legacy response identity duplicated')
            matches[key] = target_date
            payload = ingest.checked_payload(source/'jquants', ref)
            token = payload.get('pagination_key') or None
            r.require(bool(token) == (index < len(references)-1), 'legacy response incomplete')
    for name, value in state['ranges'].items():
        r.require(value['complete'], 'legacy shared response incomplete')
        pages(name, campaign['start_date'], value['query'], value['pages'])
    for task, digest in state['tasks'].items():
        name, day = task.split(':')
        manifest = json.loads(read(f'jquants/datasets/{name}/{day}/revisions/{digest}.json'))
        r.require(manifest['status'] == 'complete' and manifest['query'] == {'date': day},
                  'legacy dataset identity differs')
        pages(name, day, manifest['query'], manifest['pages'])
    used = Counter((x['endpoint'], x['query_sha256']) for b in state['batches'] for x in b)
    r.require(used == Counter({key: 1 for key in matches}),
              'legacy reservations do not match saved responses; no refund or inference')
    value = dict(version=1, source_commit=source_commit, journal_sha256=codec.digest(raw),
        campaign=campaign, payload_disposition='REJECTED_NOT_ADOPTED',
        batches=[[dict(target_date=matches[(x['endpoint'], x['query_sha256'])], receipt=copy.deepcopy(x))
                  for x in batch] for batch in state['batches']])
    validate(value, campaign)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--source-commit', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.source_commit), sort_keys=True))


if __name__ == '__main__':
    main()
