"""Date-addressed journal and shared, durable history request batches."""
import copy
from datetime import date
import math
import uuid
try:
    from scripts import actions_daily_remote as r, jquants_daily as daily, jquants_ingest as ingest
    from scripts import market_data_git as codec, jquants_calendar as calendar_validation
    from scripts.jquants_catalog import BY_NAME
except ModuleNotFoundError:
    import actions_daily_remote as r, jquants_daily as daily, jquants_ingest as ingest
    import market_data_git as codec, jquants_calendar as calendar_validation
    from jquants_catalog import BY_NAME


def day(value):
    r.require(isinstance(value, str), 'target_date must be YYYY-MM-DD')
    parsed = date.fromisoformat(value)
    r.require(parsed.isoformat() == value, 'target_date must be YYYY-MM-DD')
    return parsed


def campaign(scope):
    value = scope.get('history_campaign')
    r.require(isinstance(value, dict) and set(value) == {'start_date', 'as_of', 'sessions'},
              'explicit approved history range missing')
    start, end = day(value['start_date']), day(value['as_of'])
    r.require(0 <= (end-start).days <= 550 and type(value['sessions']) is int
              and 1 <= value['sessions'] <= 280, 'history range outside reviewed limits')
    return copy.deepcopy(value)


def validate(document):
    value = document['targets']
    r.require(set(value) == {'campaign', 'profiles', 'batches', 'ranges'}, 'invalid target ledger')
    campaign({'history_campaign': value['campaign']})
    r.require(isinstance(value['profiles'], dict) and set(value['profiles']) == set(document['runs']),
              'date budget identity missing')
    for target, profile in value['profiles'].items():
        day(target)
        r.require(profile in {'daily', 'history'}, 'invalid target profile')
        if profile == 'history':
            r.require(value['campaign']['start_date'] <= target <= value['campaign']['as_of'], 'date outside campaign')
    batches = value['batches']
    r.require(isinstance(batches, list) and 1 <= len(batches) <= 50
              and all(isinstance(b, list) and len(b) <= 12 for b in batches)
              and all(len(b) == 12 for b in batches[:-1]), '50 batch or 12 request limit exceeded')
    receipts = [v for batch in batches for v in batch]
    r.require(len(receipts) <= 600, '600 request campaign limit exceeded')
    expected = {(target, reservation) for target, run in document['runs'].items()
                if value['profiles'][target] == 'history' for reservation in run['reservation_ids']}
    actual = []
    for item in receipts:
        r.require(set(item) == {'target_date', 'reservation_id'}, 'invalid batch receipt')
        actual.append((item['target_date'], item['reservation_id']))
    r.require(len(set(actual)) == len(actual) and set(actual) == expected, 'history budget missing or duplicated')
    r.require(isinstance(value['ranges'], dict) and set(value['ranges']) <= {'trading_calendar', 'topix_daily'},
              'unexpected shared dataset')
    for name, shared in value['ranges'].items():
        r.require(set(shared) == {'query', 'pages', 'next_key', 'seen_keys', 'complete'}, 'invalid shared range')
        r.require(shared['query'] == {'from': value['campaign']['start_date'], 'to': value['campaign']['as_of']}
                  and type(shared['complete']) is bool and isinstance(shared['pages'], list)
                  and isinstance(shared['seen_keys'], list), 'shared range identity mismatch')


def transition(old, new):
    r.require(new['campaign'] == old['campaign'], 'campaign change cannot reset budget')
    r.require(all(new['profiles'].get(k) == v for k, v in old['profiles'].items()), 'target profile changed')
    before = [v for b in old['batches'] for v in b]
    after = [v for b in new['batches'] for v in b]
    r.require(after[:len(before)] == before and len(new['batches']) >= len(old['batches']), 'target budget reset')
    for name, shared in old['ranges'].items():
        updated = new['ranges'].get(name)
        r.require(updated and updated['query'] == shared['query']
                  and updated['pages'][:len(shared['pages'])] == shared['pages'], 'shared receipt rewrite')
        if shared['complete']:
            r.require(updated == shared, 'completed shared range rewrite')


class TargetCheckpoint(r.RunnerCheckpoint):
    def __init__(self, *args, target_date, profile, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_day, self.profile = day(target_date), profile

    def can_defer(self, state):
        """Only a known-success, calendar-only out-of-order journal may wait.

        Preserve its requests, reservation IDs and snapshot. No failure, unknown
        response, dated dataset work or publication is reclassified as complete.
        This also recognizes the pre-fix RUNNING/IDLE journal on cold restore.
        """
        target = state['run_date']
        run = self.document['runs'].get(target)
        shared = self.document['targets']
        calendar = shared['ranges'].get('trading_calendar')
        if (not run or run['state'] != state or run['phase'] != 'IDLE'
                or shared['profiles'].get(target) != 'history' or state['status'] != 'RUNNING'
                or state['tasks'] or state['data_date'] is not None or state['error'] is not None
                or state['http_status'] is not None or state['retry_not_before'] is not None
                or run['retry_not_before'] is not None or not calendar or not calendar['complete']):
            return False
        archive = self.source/'jquants'
        rows = [row for ref in calendar['pages'] for row in ingest.checked_payload(archive, ref)['data']]
        try:
            by_day = calendar_validation.validate(rows, shared['campaign']['start_date'],
                shared['campaign']['as_of'], complete=True, sessions=shared['campaign']['sessions'])
        except (ValueError, TypeError, KeyError):
            return False
        sessions = sorted(d for d, h in by_day.items() if h in {'1','2'})
        if target not in sessions: return False
        missing = [d for d in sessions if self.document['runs'].get(d, {}).get('phase') != 'RESTORED']
        if not missing or target <= missing[0]:
            return False
        receipts = state['requests']
        if receipts:
            queries = []
            token = None
            for ref in calendar['pages']:
                query = dict(calendar['query'])
                if token: query['pagination_key'] = token
                queries.append(ingest.digest(ingest.encoded(query)))
                token = ingest.checked_payload(archive, ref).get('pagination_key')
            index = 0
            for receipt in receipts:
                if receipt['endpoint'] != BY_NAME['trading_calendar'].endpoint: return False
                if receipt['query_sha256'] == queries[index]: continue
                if index+1 < len(queries) and receipt['query_sha256'] == queries[index+1]:
                    index += 1
                else:
                    return False
            # IDLE plus all durable response pages proves recovery from any known
            # failed attempts. Repeated query reservations stay charged; unknown
            # outcomes cannot reach IDLE and are never eligible here.
            if index != len(queries)-1: return False
        return True

    def attach(self, state):
        self.day = self.target_day.isoformat()
        r.require(state['run_date'] == self.day, 'target date differs from journal')
        r.require(all(k == self.day or v['phase'] == 'RESTORED' or self.can_defer(v['state'])
                      for k, v in self.document['runs'].items()),
                  'prior target not restored')
        runs = self.document['runs']
        profiles = self.document['targets']['profiles']
        if self.day in runs:
            r.require(runs[self.day]['state'] == state and profiles[self.day] == self.profile,
                      'target journal changed; no budget reset')
        else:
            r.require(not state['requests'], 'untracked target journal cannot be imported')
            profiles[self.day] = self.profile
            runs[self.day] = dict(state=copy.deepcopy(state), phase='IDLE', retry_not_before=None, reservation_ids=[])
            self.sync(state)

    def before_reservation(self):
        super().before_reservation()
        if self.profile == 'history':
            batches = self.document['targets']['batches']
            if sum(map(len, batches)) >= 600 or len(batches) == 50 and len(batches[-1]) == 12:
                raise ingest.RequestBudgetExhausted('history campaign exhausted')

    def before_provider_io(self):
        if self.authorization.get('handover_check'): self.authorization['handover_check']()
        r.gate(self.authorization, now=self.clock())
        r.require(self.document['runs'][self.day]['phase'] == 'IN_FLIGHT', 'provider reservation missing')
        now = self.clock().astimezone(daily.JST).date()
        r.require(self.target_day <= now, 'future target at send boundary')
        # A latest-day collection that crosses midnight keeps its charged journal
        # and stops. A later invocation can resume the same reviewed daily profile.
        r.require(now == self.execution_day, 'operating JST day changed after reservation')

    def prepare(self, source):
        self.execution_day = self.clock().astimezone(daily.JST).date()
        super().prepare(source)
        shared = self.document['targets']
        path = self.source/'jquants/target-history/ranges.json'
        if shared['ranges']:
            r.require(path.is_file() and ingest.read_json(path) == dict(campaign=shared['campaign'],
                ranges=shared['ranges'], knowledge='retrieved_now_not_point_in_time'),
                'shared archive provenance differs from ledger')
        else:
            r.require(not path.exists(), 'untracked shared campaign cannot be imported')

    def reserve(self, state):
        run = self.document['runs'][self.day]
        r.require(not self.poisoned and run['phase'] in {'IDLE', 'PAUSED'}, 'unknown or blocked target outcome')
        if run['retry_not_before']:
            r.require(self.clock() >= daily.aware(run['retry_not_before']), 'target retry cooldown pending')
        old = run['state']
        r.require(state['run_date'] == self.day and state['requests'][:-1] == old['requests']
                  and len(state['requests']) == len(old['requests']) + 1 and len(state['requests']) <= 12
                  and state['budget'] == old['budget'] and state['config_sha256'] == old['config_sha256'],
                  'persistent target budget exhausted or changed')
        reservation = uuid.uuid4().hex
        if self.profile == 'history':
            batches = self.document['targets']['batches']
            r.require(sum(map(len, batches)) < 600, '600 request campaign limit exceeded')
            if len(batches[-1]) == 12:
                r.require(len(batches) < 50, '50 batch limit exceeded')
                batches.append([])
            batches[-1].append(dict(target_date=self.day, reservation_id=reservation))
        run['state'] = copy.deepcopy(state)
        run['reservation_ids'].append(reservation)
        run['phase'] = 'IN_FLIGHT'
        self.document['snapshot'] = self.capture_snapshot()
        self.commit()

    def collect_history(self, state, archive, client, fetch, save):
        shared = self.document['targets']
        def rows(name):
            value = shared['ranges'].setdefault(name, dict(query={'from': shared['campaign']['start_date'],
                'to': shared['campaign']['as_of']}, pages=[], next_key=None, seen_keys=[], complete=False))
            ingest.atomic_json(archive/'target-history/ranges.json',
                dict(campaign=shared['campaign'], ranges=shared['ranges'],
                     knowledge='retrieved_now_not_point_in_time'))
            # Validate every saved page and pagination link on every cold resume.
            seen = []
            for index, ref in enumerate(value['pages']):
                payload = ingest.checked_payload(archive, ref)
                token = payload.get('pagination_key') or None
                r.require(isinstance(payload.get('data'), list) and (bool(token) or index == len(value['pages'])-1),
                          'shared pagination corrupt')
                if token:
                    r.require(isinstance(token, str) and token not in seen, 'shared pagination cycle')
                    seen.append(token)
            last = (ingest.checked_payload(archive, value['pages'][-1]).get('pagination_key') or None) if value['pages'] else None
            r.require(seen == value['seen_keys'] and last == value['next_key']
                      and value['complete'] == (bool(value['pages']) and last is None), 'shared checkpoint differs')
            while not value['complete']:
                query = dict(value['query'])
                if value['next_key']: query['pagination_key'] = value['next_key']
                payload = client().get(BY_NAME[name].endpoint, query)
                token = payload.get('pagination_key')
                r.require(isinstance(payload.get('data'), list) and all(isinstance(v, dict) for v in payload['data'])
                          and (token is None or isinstance(token, str))
                          and (not token or token not in value['seen_keys']), 'invalid shared provider response')
                ref = ingest.save_object(archive, payload)
                ref['retrieved_at'] = self.clock().isoformat()
                value['pages'].append(ref)
                value['next_key'] = token or None
                if token: value['seen_keys'].append(token)
                value['complete'] = not bool(token)
                ingest.atomic_json(archive/'target-history/ranges.json',
                    dict(campaign=shared['campaign'], ranges=shared['ranges'],
                         knowledge='retrieved_now_not_point_in_time'))
                self.page_saved(state)
            result = [row for ref in value['pages'] for row in ingest.checked_payload(archive, ref)['data']]
            r.require(all(value['query']['from'] <= row.get('Date', '') <= value['query']['to'] for row in result),
                      'shared response date outside approved range')
            return result
        calendar = rows('trading_calendar')
        by_day = calendar_validation.validate(calendar, shared['campaign']['start_date'],
            shared['campaign']['as_of'], complete=True, sessions=shared['campaign']['sessions'])
        sessions = sorted(k for k, v in by_day.items() if v in {'1','2'})
        if by_day[self.day] in {'0','3'}:
            state['status'] = 'HOLIDAY'
            return
        missing = [d for d in sessions if d not in self.document['runs'] or self.document['runs'][d]['phase'] != 'RESTORED']
        if missing and missing[0] != self.day:
            self.wait_for_target = missing[0]
            return  # retain RUNNING/IDLE and every charged calendar receipt
        topix = rows('topix_daily')
        r.require(len({v['Date'] for v in topix}) == len(topix) and set(sessions) <= {v['Date'] for v in topix},
                  'TOPIX session missing or duplicated')
        r.require(all(all(not isinstance(v.get(k), bool) and math.isfinite(float(v[k])) and float(v[k]) > 0
                          for k in ('O','H','L','C')) for v in topix), 'invalid TOPIX values')
        state['data_date'] = self.day
        for name in ('equities_master', 'equities_daily'): fetch(name, self.target_day)
        state['status'] = 'COLLECTED'


def verify_adopted_targets(document, expected, adopted):
    """Check restored dates against the CURRENT main, not just checkpoint flags.

    Later generations may append dates. Every adopted target's immutable evidence
    and canonical price bytes must still exist unchanged in the selected generation.
    Unpublished waiting/partial journals are deliberately not claimed as adopted.
    """
    required = set()
    shared_needed = set()
    for text, run in document['runs'].items():
        if run['phase'] != 'RESTORED': continue
        state = run['state']
        journal = daily.run_path(expected/'jquants', day(text))
        r.require(journal.is_file() and daily.read_state(journal) == state, 'restored target journal differs from ledger')
        required.add(journal.relative_to(expected).as_posix())
        for task, receipt in state['tasks'].items():
            dataset, target = task.split(':')
            path = f"jquants/datasets/{dataset}/{target}/revisions/{receipt['manifest_sha256']}.json"
            required.add(path)
            manifest = ingest.read_json(expected/path)
            required.update('jquants/'+p['path'] for p in manifest['pages'])
        if state['status'] == 'COLLECTED':
            revisions = expected/'daily-prices/revisions'/text
            files = [p for p in revisions.rglob('*') if p.is_file()]
            r.require(any(p.name.startswith('manifest-') for p in files)
                      and any(p.suffix == '.csv' for p in files), 'restored target normalized evidence missing')
            required.update(p.relative_to(expected).as_posix() for p in files)
            required.add(f'daily-prices/{text[:4]}/{text}.csv')
        if document['targets']['profiles'][text] == 'history':
            shared_needed.add('trading_calendar')
            if state['status'] == 'COLLECTED': shared_needed.add('topix_daily')
    if shared_needed:
        path = adopted/'jquants/target-history/ranges.json'
        r.require(path.is_file(), 'current main lost adopted shared provenance')
        actual = ingest.read_json(path)
        r.require(actual['campaign'] == document['targets']['campaign'], 'current main shared campaign differs')
        for name in shared_needed:
            value = document['targets']['ranges'][name]
            r.require(value['complete'] and actual['ranges'].get(name) == value,
                      'current main lost adopted shared dataset')
            required.update('jquants/'+p['path'] for p in value['pages'])
    for name in sorted(required):
        wanted, actual = expected/name, adopted/name
        r.require(wanted.is_file() and actual.is_file() and codec.digest(wanted.read_bytes()) == codec.digest(actual.read_bytes()),
                  'current main lost or changed restored target evidence; review required')
