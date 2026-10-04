"""One-use, attempt-bound acquisition grants. No network or credential discovery.

The full allowance is adopted on main BEFORE provider I/O. It is never refunded,
including when a result branch or Actions history disappears. Receipts distinguish
observed sends from the conservative charged allowance.
"""
import copy
from datetime import date
import re
try:
    from scripts import actions_daily_remote as r, market_data_git as codec
    from scripts import actions_legacy_debit as debit, jquants_snapshot as snapshot
    from scripts.jquants_catalog import BY_NAME, query_for, period
except ModuleNotFoundError:
    import actions_daily_remote as r, market_data_git as codec, actions_legacy_debit as debit, jquants_snapshot as snapshot
    from jquants_catalog import BY_NAME, query_for, period

ROOT = 'stock.jp/data/date-acquisition/'
LEDGER = ROOT + 'ledger.json'
DATASETS = ('trading_calendar', 'equities_master', 'equities_daily')


def day(value):
    r.require(isinstance(value, str) and date.fromisoformat(value).isoformat() == value, 'invalid target date')
    return value


def check_target(target, campaign, today):
    day(target)
    r.require(campaign['start_date'] <= target <= campaign['as_of'] and target <= today.isoformat(),
              'target outside reviewed campaign')
    requested = date.fromisoformat(target)
    r.require(all(period(BY_NAME[name], 'light', today)[0] <= requested <= period(BY_NAME[name], 'light', today)[1]
                  for name in DATASETS), 'target outside Light history entitlement')


def validate(ledger):
    r.require(isinstance(ledger, dict) and set(ledger) == {'version', 'legacy_debit', 'prior_charges', 'prior_batches', 'grants'}, 'invalid date ledger')
    r.require(ledger['version'] == 1, 'unsupported date ledger')
    debit.validate(ledger['legacy_debit'], ledger['legacy_debit']['campaign'])
    prior = ledger['prior_charges']
    r.require(isinstance(prior, dict) and type(ledger['prior_batches']) is int and ledger['prior_batches'] >= 0, 'prior costs missing')
    for target, cost in prior.items():
        day(target)
        r.require(type(cost) is int and 0 < cost <= 12, 'invalid prior cost')
    grants = ledger['grants']
    r.require(isinstance(grants, list), 'invalid grant history')
    seen, attempts = set(), set()
    for grant in grants:
        r.require(isinstance(grant, dict) and set(grant) == {'date', 'run_id', 'attempt', 'head_sha', 'allowance'}, 'invalid grant')
        day(grant['date']); r.sha(grant['head_sha'])
        r.require(all(type(grant[k]) is int and grant[k] > 0 for k in ('run_id', 'attempt', 'allowance')), 'invalid grant identity')
        r.require(grant['date'] not in seen and grant['date'] not in prior and (grant['run_id'], grant['attempt']) not in attempts, 'duplicate acquisition grant')
        seen.add(grant['date']); attempts.add((grant['run_id'], grant['attempt']))
        r.require(grant['allowance'] + debit.charges(ledger['legacy_debit'], grant['date']) <= 12, 'daily limit exceeded')
    r.require(debit.charges(ledger['legacy_debit']) + sum(prior.values()) + sum(g['allowance'] for g in grants) <= 600, 'campaign request limit exceeded')
    r.require(len(ledger['legacy_debit']['batches']) + ledger['prior_batches'] + len(grants) <= 50, 'batch limit exceeded')
    r.require(len(set(prior) | seen | {e['target_date'] for b in ledger['legacy_debit']['batches'] for e in b}) <= 280, 'date limit exceeded')
    codec.scan(codec.encoded(ledger))


def extends(before, after):
    validate(before); validate(after)
    r.require(all(before[k] == after[k] for k in ('version', 'legacy_debit', 'prior_charges', 'prior_batches'))
              and after['grants'][:len(before['grants'])] == before['grants'], 'adopted budget cannot be removed or rewritten')


def propose(ledger, target, context):
    validate(ledger); day(target)
    r.require(target not in ledger['prior_charges'] and not any(g['date'] == target for g in ledger['grants']), 'date already charged; no replacement grant')
    grant = dict(date=target, run_id=context['run_id'], attempt=context['run_attempt'] + 1,
                 head_sha=context['sha'], allowance=12-debit.charges(ledger['legacy_debit'], target))
    result = copy.deepcopy(ledger); result['grants'].append(grant); validate(result)
    return result, grant


def result_prefix(grant):
    return ROOT + grant['date'] + '/'


def raw_quality(rows):
    """Check raw response sufficiency, never financial/readiness coverage.

    Use the existing dated domestic-common-stock universe. Every expected issue
    needs an explicit structurally usable bar (including explicit no-execution
    nulls). This does not certify the pinned 98% valid-price or active-target gates.
    """
    master, daily = rows.get('equities_master', []), rows.get('equities_daily', [])
    result = dict(status='INCOMPLETE', reason=None, master_rows=len(master), daily_rows=len(daily),
                  analysis_readiness='NOT_EVALUATED')
    if not master or not daily:
        result['reason'] = 'EMPTY_REQUIRED_DATASET'; return result
    codes, expected, bars = set(), set(), {}
    for row in master:
        raw = str(row.get('Code') or '').strip().upper()
        if not re.fullmatch(r'[0-9A-Z]{4,5}', raw) or not row.get('ProdCat'):
            result['reason'] = 'INVALID_MASTER_ROW'; return result
        code = snapshot.normalize_code(raw)
        if code in codes:
            result['reason'] = 'DUPLICATE_MASTER_CODE'; return result
        codes.add(code)
        if str(row['ProdCat']) != '011' or len(raw) == 5 and not raw.endswith('0'): continue
        market = str(row.get('Mkt') or '')
        if market in snapshot.EXCLUDED_MARKETS: continue
        if market not in snapshot.ELIGIBLE_MARKETS:
            result['reason'] = 'INVALID_DATED_MARKET'; return result
        expected.add(code)
    if not expected:
        result['reason'] = 'EMPTY_DATED_UNIVERSE'; return result
    for row in daily:
        raw = str(row.get('Code') or '').strip().upper()
        if not re.fullmatch(r'[0-9A-Z]{4,5}', raw):
            result['reason'] = 'INVALID_BAR_CODE'; return result
        code = snapshot.normalize_code(raw)
        if code in bars or code not in codes:
            result['reason'] = 'DUPLICATE_OR_UNKNOWN_BAR_CODE'; return result
        bars[code] = row
    result['expected_common_stocks'] = len(expected)
    result['missing_bar_rows'] = len(expected-bars.keys())
    if result['missing_bar_rows']:
        result['reason'] = 'MISSING_REQUIRED_BAR_ROWS'; return result
    if any(snapshot.bar_diagnostic(bars[c]) == 'PARTIAL_NULL_OR_INVALID' for c in expected):
        result['reason'] = 'INSUFFICIENT_BAR_FIELDS'; return result
    result.update(status='COMPLETE', reason=None)
    return result


def execute(grant, context, provider, save, observe, clock, sleep):
    """save must durably acknowledge all files before returning; failure stops.

    One attempt executes once. No retries, resumed sends, replacement grants or
    automatic budget refunds. Every page gets a durable PENDING receipt first.
    Caller must reject an existing result branch before entering this function.
    """
    r.require((grant['run_id'], grant['attempt'], grant['head_sha']) ==
              (context['run_id'], context['run_attempt'], context['sha']), 'grant belongs to another run attempt')
    state = dict(version=1, grant=copy.deepcopy(grant), status='COLLECTING', requests=[], datasets={}, analysis_readiness='NOT_EVALUATED')
    prefix = result_prefix(grant)
    def persist(extra=None):
        save({prefix+'result.json': codec.encoded(state), **(extra or {})})
    persist()
    collected_rows = {}
    for name in DATASETS:
        query = query_for(BY_NAME[name], date.fromisoformat(grant['date']))
        tokens, rows, references = set(), [], []
        while True:
            r.require(len(state['requests']) < grant['allowance'], 'reserved request allowance exhausted')
            receipt = dict(dataset=name, query=copy.deepcopy(query), reserved_at=clock().isoformat(), outcome='PENDING')
            state['requests'].append(receipt); persist()
            # Verify live owner, unchanged adopted main, branch head and expiry
            # AFTER the durable reservation and immediately before each send.
            sleep(1.1); observe()
            try:
                payload = provider(BY_NAME[name].endpoint, query)
                r.require(isinstance(payload, dict) and isinstance(payload.get('data'), list)
                          and all(isinstance(row, dict) and row.get('Date') == grant['date'] for row in payload['data']), 'invalid provider rows or date')
                token = payload.get('pagination_key') or None
                r.require(token is None or isinstance(token, str) and len(token) <= 4096 and token not in tokens, 'invalid or repeated pagination token')
                raw = codec.encoded(payload); codec.scan(raw)
                r.require(len(raw) <= 8*1024*1024, 'provider page too large')
            except Exception:
                # Even a known rejection consumes the adopted allowance. No
                # automatic resend after 429, timeout, parsing or write failure.
                receipt['outcome'] = 'FAILED_OR_UNKNOWN'; state['status'] = 'BLOCKED'
                persist()
                raise r.RemoteStop('provider failed or outcome unknown; grant remains charged') from None
            path = prefix + name + '/' + str(len(references)+1).zfill(3) + '.json'
            receipt.update(outcome='SAVED', path=path, sha256=codec.digest(raw), row_count=len(payload['data']))
            references.append(path); rows.extend(payload['data'])
            state['datasets'][name] = dict(pages=list(references), row_count=len(rows), complete=token is None)
            persist({path:raw})
            if token is None: break
            tokens.add(token); query['pagination_key'] = token
        collected_rows[name] = rows
        if name == 'trading_calendar':
            r.require(len(rows) == 1 and str(rows[0].get('HolDiv')) in {'0','1','2','3'}, 'invalid trading day evidence')
            if str(rows[0]['HolDiv']) in {'0','3'}:
                state['status'] = 'HOLIDAY'; persist(); return state
    state['raw_quality'] = raw_quality(collected_rows)
    state['status'] = 'RAW_COLLECTED' if state['raw_quality']['status'] == 'COMPLETE' else 'INCOMPLETE'
    persist()
    return state


def verify_result(state, grant, read):
    r.require(state.get('version') == 1 and state.get('grant') == grant
              and state.get('status') in {'RAW_COLLECTED','HOLIDAY'}
              and state.get('analysis_readiness') == 'NOT_EVALUATED', 'result not complete')
    requests = state.get('requests')
    r.require(isinstance(requests, list) and 0 < len(requests) <= grant['allowance'], 'result receipts invalid')
    expected_names = DATASETS[:1] if state['status'] == 'HOLIDAY' else DATASETS
    expected_queries = {n:query_for(BY_NAME[n], date.fromisoformat(grant['date'])) for n in expected_names}
    rows, paths, tokens = {n:[] for n in expected_names}, {n:[] for n in expected_names}, {}
    for receipt in requests:
        name = receipt.get('dataset')
        r.require(name in expected_names and receipt.get('outcome') == 'SAVED', 'result has unresolved sends')
        query = expected_queries[name]
        r.require(name not in tokens or tokens[name] is not None, 'extra page after completion')
        if tokens.get(name): query = {**query, 'pagination_key':tokens[name]}
        r.require(receipt['query'] == query, 'result query mismatch')
        path = result_prefix(grant)+name+'/'+str(len(paths[name])+1).zfill(3)+'.json'
        r.require(receipt['path'] == path, 'result path mismatch')
        raw = read(path); codec.scan(raw)
        r.require(codec.digest(raw) == receipt['sha256'], 'result hash mismatch')
        import json
        payload = json.loads(raw)
        r.require(isinstance(payload.get('data'), list) and len(payload['data']) == receipt['row_count']
                  and all(isinstance(row, dict) and row.get('Date') == grant['date'] for row in payload['data']), 'result rows mismatch')
        rows[name].extend(payload['data']); paths[name].append(path)
        tokens[name] = payload.get('pagination_key') or None
    r.require(all(paths[n] and tokens[n] is None and state['datasets'][n] ==
              dict(pages=paths[n], row_count=len(rows[n]), complete=True) for n in expected_names), 'result incomplete')
    calendar = rows['trading_calendar']
    r.require(len(calendar) == 1 and str(calendar[0].get('HolDiv')) in
              ({'0','3'} if state['status'] == 'HOLIDAY' else {'1','2'}), 'holiday status mismatch')
    if state['status'] == 'RAW_COLLECTED':
        quality = raw_quality(rows)
        r.require(quality['status'] == 'COMPLETE' and state.get('raw_quality') == quality,
                  'raw result is insufficient; analysis readiness remains unevaluated')
    return state
