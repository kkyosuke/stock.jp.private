"""PAPER-only calendar alignment; the frozen collector and liquidity formula stay intact."""
from datetime import date, datetime, time
import csv
import gzip
import hashlib
import json
import statistics

from stock_jp.market_data import jpx
from stock_jp.market_data.jquants import ingest
from stock_jp.strategy.liquidity import trading_value_window


def _require(value, message):
    if not value:
        raise ValueError(message)


def apply(*, root, run_id, target, cutoff, observed, targets, config, run_dir, result):
    """Rebuild only liquidity from the collector's hash-checked bars and calendar.

    Called only by the bound PAPER operation, never by frozen research. No HTTP,
    price imputation, session skipping, or historical-availability certification.
    """
    manifest_path = run_dir / 'reference-data/manifest.json'
    manifest = json.loads(manifest_path.read_text())
    providers = result['providers']
    market = providers.get('jquants_market_data', {})
    audit = {'version': 1, 'adapter': 'paper-adopted-session-v1', 'run_id': run_id,
             'cutoff_at_jst': cutoff.isoformat(timespec='seconds'),
             'observed_at_jst': observed.isoformat(timespec='seconds'),
             'historical_point_in_time_certified': False,
             'original_provider': manifest.get('paper_liquidity_adapter', {}).get('original_provider', dict(market)), 'status': 'BLOCKED'}
    rows, sources = [], []
    try:
        policy = json.loads((root / 'config/policy.json').read_text())
        _require(policy['operation_mode'] == 'PAPER' and policy['broker_submission'] == 'HUMAN_ONLY',
                 'liquidity adapter requires PAPER/HUMAN_ONLY')
        _require(target['day'] == run_id and target['daily_status'] == 'COLLECTED',
                 'liquidity target must be an adopted trading day')
        day = date.fromisoformat(run_id)
        _require(cutoff.tzinfo is not None and observed.tzinfo is not None and cutoff <= observed,
                 'invalid cutoff or observation time')
        _require(target['cutoff_at_jst'] == cutoff.isoformat(timespec='seconds'), 'bound cutoff differs')
        _require(datetime.combine(day, time(15, 30), tzinfo=jpx.JST) <= cutoff,
                 'target session has not closed at cutoff')
        record_path = root / f'operations/acquisition/nightly/{run_id}.json'
        raw = record_path.read_bytes()
        _require(hashlib.sha256(raw).hexdigest() == target['adoption_record_sha256'],
                 'adopted record differs from bound main evidence')
        record = json.loads(raw)
        _require(record['status'] == 'COLLECTED' and record['run_date'] == run_id and record['data_date'] == run_id,
                 'target acquisition is not complete')
        _require(record['config_sha256'] == ingest.digest((root / 'config/sources.json').read_bytes()),
                 'adopted source configuration differs')
        _require(record['requests'] and all(_aware(r['reserved_at']) <= cutoff for r in record['requests']),
                 'adopted observation is later than cutoff')
        _require(market.get('status') in {'OK', 'INCOMPLETE'},
                 'transport or provider failure cannot be repaired by calendar alignment')
        _require(manifest['run_id'] == run_id and manifest['cutoff_at_jst'] == target['cutoff_at_jst'],
                 'source manifest identity differs')
        _require(_aware(manifest['retrieved_at_jst']) <= observed, 'source observation is in the future')
        audit['adoption_record_sha256'] = target['adoption_record_sha256']
        audit['source_retrieved_at_jst'] = manifest['retrieved_at_jst']
        audit['observation_time_semantics'] = 'collector scan-start label; origin query receipts retained, not exact HTTP completion times'
        audit['source_observed_after_cutoff'] = _aware(manifest['retrieved_at_jst']) > cutoff
        audit['query_receipts'] = manifest.get('jquants_checkpoint', {}).get('pages', [])
        for receipt in audit['query_receipts']:
            _require(_aware(receipt['retrieved_at_jst']) <= observed, 'query observation is in the future')
            audit['source_observed_after_cutoff'] |= _aware(receipt['retrieved_at_jst']) > cutoff

        def checked(name):
            entry = manifest['datasets'][name]
            path = ingest.confined(run_dir, entry['path'])
            _require(ingest.digest(path.read_bytes()) == entry['sha256'], 'source dataset checksum differs')
            return path

        calendar = json.loads(checked('trading_calendar').read_text())['rows']
        _require(providers['jquants_trading_calendar']['status'] == 'OK', 'official calendar incomplete')
        _require([r['holiday_division'] for r in calendar if r['date'] == run_id] in (['1'], ['2']),
                 'target is not an official trading session')
        with gzip.open(checked('jquants_daily_bars'), 'rt', newline='') as stream:
            normalized = list(csv.DictReader(stream))
        bars = []
        for code in sorted(targets):
            raw_path = run_dir / f'raw-sources/jquants-bars-{code}-{run_id}.json'
            pages = json.loads(raw_path.read_text())['pages']
            _require(pages and not pages[-1].get('pagination_key'), 'daily bars query incomplete')
            bars.extend(row for page in pages for row in page['data'])
        _require(jpx._normalize_bars(bars) == normalized, 'raw bars differ from hash-checked dataset')
        _require(all(date.fromisoformat(row['Date']) <= day for row in bars), 'future bar beyond adopted target')
        sessions = config['jquants'].get('minimum_liquidity_sessions', 20)
        for code in sorted(targets):
            window, values = trading_value_window(bars, calendar, code=code, cutoff=day, sessions=sessions)
            latest = next(r for r in normalized if r['code'] == code and r['date'] == window[-1])
            _require((cutoff.date() - day).days <= config['jquants'].get('maximum_official_price_age_days', 7),
                     'latest official bar is stale')
            evidence = manifest['datasets']['jquants_daily_bars']['path']
            rows.append({'as_of_date': latest['date'], 'code': code, 'sessions': str(len(window)),
                         'average_trading_value_20d': format(statistics.fmean(values), '.12g'),
                         'median_trading_value_20d': format(statistics.median(values), '.12g'),
                         'latest_close': latest['close'], 'latest_volume': latest['volume'],
                         'latest_trading_value': latest['trading_value'], 'provider': 'J-Quants API V2',
                         'evidence_path': evidence})
            sources.append(jpx._source_row(source_id=f'jquants-price-{run_id}-{code}', category='market_data', code=code,
                title=f'Official daily price and 20-session liquidity for {code}', published_at=f'{run_id}T15:30:00+09:00',
                retrieved_at=manifest['retrieved_at_jst'], url=manifest['datasets']['jquants_daily_bars']['source_url'],
                notes='PAPER adopted-session alignment; unchanged 20-session formula; retrieval time retained; not historical PIT certification'))
        audit.update(status='ALIGNED', window_end=run_id, sessions=20)
    except (KeyError, ValueError, TypeError, OSError) as error:
        audit['error'] = str(error)
        rows, sources = [], []

    # A failed adapter cannot leave an earlier successful liquidity result usable.
    path = run_dir / 'reference-data/liquidity-20d.csv'
    jpx._atomic_csv(path, jpx.LIQUIDITY_FIELDS, rows)
    manifest['datasets']['liquidity_20d'] = {**jpx._manifest_entry(path, run_dir, len(rows),
        manifest.get('datasets', {}).get('jquants_daily_bars', {}).get('source_url', 'https://api.jquants.com/v2/equities/bars/daily')),
        'calculation': 'last 20 consecutive official cash-equity sessions ending on adopted target; unchanged formula'}
    manifest['paper_liquidity_adapter'] = audit
    jpx._atomic_json(manifest_path, manifest)
    result['source_rows'] = [r for r in result['source_rows'] if not r['source_id'].startswith('jquants-price-')] + sources
    market.update(status='OK' if audit['status'] == 'ALIGNED' else 'INCOMPLETE', error=audit.get('error', ''))
    providers['jquants_market_data'] = market
    if audit['status'] == 'ALIGNED':
        result['successful_gap_sources'].add('official_market_data')
        # Clear only the aggregate query gap when every enabled provider is good.
        flags = {'jquants_security_master': 'listed_master_enabled', 'jquants_trading_calendar': 'trading_calendar_enabled',
                 'jquants_market_data': 'daily_bars_enabled', 'jquants_financials': 'financial_summary_enabled',
                 'jquants_earnings_calendar': 'earnings_calendar_enabled'}
        if all(providers.get(name, {}).get('status') in {'OK', 'NOT_APPLICABLE'}
               for name, flag in flags.items() if config['jquants'].get(flag, True)):
            result['blocking_failures'] = [f for f in result['blocking_failures'] if f['source'] != 'nightly_jquants_scan']
            result['successful_gap_sources'].add('nightly_jquants_scan')
    else:
        result['successful_gap_sources'].discard('official_market_data')
        result['successful_gap_sources'].discard('nightly_jquants_scan')
        result['blocking_failures'].append({'source': 'official_market_data', 'impact': audit['error'],
            'reason': 'Review the adopted target and saved official observations; do not impute missing prices'})
    return result


def _aware(value):
    parsed = datetime.fromisoformat(value)
    _require(parsed.tzinfo is not None, 'observation timestamp needs UTC offset')
    return parsed
