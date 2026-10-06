"""Read verified official index originals; never download or backdate availability."""
from datetime import date, datetime, time, timedelta
import json
import math
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_jp.market_data.jquants import ingest
from stock_jp.market_data.jquants.calendar import validate as validate_calendar
from stock_jp.market_data.regime_sources import MarketRegimeSourceError
from stock_jp.research.financial_inputs import FrozenStore

OBSERVATION = 'jquants/index-history/legacy-observation.json'
JST = ZoneInfo('Asia/Tokyo')


def require(value, reason):
    if not value:
        raise MarketRegimeSourceError(reason)


def aware(text):
    value = datetime.fromisoformat(text)
    require(value.tzinfo is not None, 'index observation needs a timezone')
    return value


def validate_observation(document, read):
    require(set(document) == {'version', 'observed_at', 'ranges'} and document['version'] == 1,
            'invalid legacy observation')
    aware(document['observed_at'])
    require(set(document['ranges']) == {'topix_daily', 'trading_calendar'}, 'legacy sources missing')
    for name, value in document['ranges'].items():
        require(value.get('complete') is True and value.get('pages'), 'legacy range incomplete')
        start, end = value['query']['from'], value['query']['to']
        require(date.fromisoformat(start) <= date.fromisoformat(end), 'invalid legacy range')
        rows = []
        for ref in value['pages']:
            require(isinstance(ref.get('sha256'), str) and re.fullmatch('[0-9a-f]{64}', ref['sha256']), 'invalid index hash')
            require(ref['path'] == f"objects/{ref['sha256'][:2]}/{ref['sha256']}.json", 'unsafe index object')
            raw = read('jquants/' + ref['path'])
            require(len(raw) == ref['bytes'] and ingest.digest(raw) == ref['sha256'], 'index checksum mismatch')
            rows.extend(json.loads(raw)['data'])
        if name == 'trading_calendar':
            validate_calendar(rows, start, end, complete=True)
        else:
            dates = [r['Date'] for r in rows]
            require(len(set(dates)) == len(dates) and all(start <= d <= end for d in dates), 'invalid legacy index dates')
    return document


def observe_legacy(archive):
    """Old range pages have no retrieval timestamps. Record first verified observation now."""
    path = archive.parent / OBSERVATION
    read = lambda n: (archive.parent / n).read_bytes()
    if path.exists():
        return validate_observation(ingest.read_json(path), read)
    state = ingest.read_json(archive / 'history/state.json')
    if not state or state.get('status') != 'COMPLETE':
        return None
    document = {'version': 1, 'observed_at': ingest.utc_now(),
                'ranges': {k: state['ranges'][k] for k in ('topix_daily', 'trading_calendar')}}
    validate_observation(document, read)
    ingest.atomic_json(path, document)
    return document


def _source(root):
    from stock_jp.workspace import record_path
    folder = record_path(root, 'market-data')
    if folder.exists():
        names = {p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}
        return names, lambda n: (folder / n).read_bytes()
    pointer = json.loads((root / 'data/market/jquants/current.json').read_text())
    store = FrozenStore(root, pointer['generation'])
    return set(store.files), store.read


def load_index_series(root, *, as_of, cutoff, require_point_in_time=True):
    """Exactly 200 official sessions, with independent observation-time evidence."""
    require(cutoff.tzinfo is not None and cutoff.astimezone(JST).date() == as_of, 'index cutoff/date mismatch')
    names, read = _source(Path(root))
    calendars, points = {}, {'topix': {}, 'growth250': {}}
    observations, evidence = [], []

    def add(kind, rows, observed, ref):
        instant = aware(observed)
        local = set()
        for row in rows:
            day = row['Date']
            parsed = date.fromisoformat(day)
            require(day not in local, 'duplicate index/calendar source date')
            local.add(day)
            if parsed > as_of:
                continue
            if kind == 'calendar':
                value = str(row['HolDiv'])
                require(value in {'0','1','2','3'}, 'invalid official calendar')
                if day in calendars:
                    require(calendars[day] == value, 'conflicting official calendars')
                calendars[day] = value
                observations.append(instant)
                continue
            value = row.get('C')
            require(type(value) in (int,float) and math.isfinite(value) and value > 0, 'invalid index close')
            prior = points[kind].get(day)
            if prior:
                require(prior[0] == value, 'conflicting index generations require review')
                instant_for_day = min(prior[1], instant)
            else:
                instant_for_day = instant
            points[kind][day] = (float(value), instant_for_day)
        evidence.append(ref)

    if OBSERVATION in names:
        legacy = validate_observation(json.loads(read(OBSERVATION)), read)
        for key, value in legacy['ranges'].items():
            rows = [r for p in value['pages'] for r in json.loads(read('jquants/' + p['path']))['data']]
            add('calendar' if key == 'trading_calendar' else 'topix', rows, legacy['observed_at'], OBSERVATION)
    bootstrap = 'jquants/index-history/growth250.json'
    if bootstrap in names:
        from stock_jp.market_data.jquants.index_history import validate
        document = json.loads(read(bootstrap))
        validate(document, read)
        if document['complete']:
            for page in document['pages']:
                add('growth250', json.loads(read('jquants/' + page['path']))['data'], page['retrieved_at'], bootstrap)
    for name in sorted(names):
        if not (name.endswith('/latest.json') or '/revisions/' in name) or not any(name.startswith('jquants/datasets/'+k+'/') for k in ('topix_daily','indices_daily','trading_calendar')):
            continue
        raw = read(name)
        document = json.loads(raw)
        revision = name if '/revisions/' in name else name.removesuffix('latest.json') + 'revisions/' + ingest.digest(raw) + '.json'
        require(revision in names and read(revision) == raw, 'index revision missing')
        from stock_jp.market_data.jquants.validation import validate_archive
        members = {name, revision} | {'jquants/' + p['path'] for p in document['pages']}
        validate_archive(members, read)
        kind = {'topix_daily':'topix','indices_daily':'growth250','trading_calendar':'calendar'}[document['dataset']]
        require(document['status'] == 'complete', 'index partition incomplete')
        seen = set()
        for index, page in enumerate(document['pages']):
            payload = read('jquants/' + page['path'])
            require(ingest.digest(payload) == page['sha256'] and len(payload) == page['bytes'], 'index page checksum mismatch')
            data = json.loads(payload)
            require(bool(data.get('pagination_key')) == (index < len(document['pages'])-1), 'index pages incomplete')
            rows = [r for r in data['data'] if kind != 'growth250' or r.get('Code') == '0070']
            require(not seen.intersection(r['Date'] for r in rows), 'duplicate dates across index pages')
            seen.update(r['Date'] for r in rows)
            require(all(r['Date'] == name.split('/')[3] for r in rows), 'index partition date mismatch')
            add(kind, rows, page['retrieved_at'], revision)
    require(calendars.get(as_of.isoformat()) in {'1','2'}, 'MRS as_of is not a confirmed trading session')
    sessions, day = [], as_of
    for _ in range(550):
        key = day.isoformat()
        require(key in calendars, 'official calendar day missing')
        if calendars[key] in {'1','2'}:
            sessions.append(key)
        if len(sessions) == 200:
            break
        day -= timedelta(days=1)
    require(len(sessions) == 200, '200 official sessions unavailable')
    if require_point_in_time:
        require(all(value <= cutoff for value in observations), 'calendar originals observed after cutoff')
    result = {}
    for kind, values in points.items():
        require(all(day in values for day in sessions), kind + ' missing an official session')
        selected = [values[day] for day in sessions]
        pit = all(observed <= cutoff for _, observed in selected)
        if require_point_in_time:
            require(pit, kind + ' originals were first observed after the evaluation cutoff; retrospective only')
        result[kind] = {'points':[{'date':day,'value':values[day][0]} for day in sorted(sessions)],
                        'primary_source':True, 'point_in_time_verified':pit,
                        'source_url': 'https://api.jquants.com/v2/indices/bars/daily' + ('/topix' if kind=='topix' else ''),
                        'evidence_paths':sorted(set(evidence))}
    return result


def collect_market_regime_series(*, root, as_of, config, cutoff_at=None):
    """Use adopted index originals and the unchanged official macro extractors."""
    from stock_jp.market_data import regime_sources as macro
    cutoff = macro._information_cutoff(as_of, cutoff_at or datetime.combine(as_of, time(15, 30), JST))
    indices = load_index_series(root, as_of=as_of, cutoff=cutoff)
    market, timeout = config['market_regime'], int(config.get('request_timeout_seconds',30))
    vi, vi_url = macro._nikkei_vi_points(market['nikkei_vi_csv_url'], as_of=as_of, timeout=timeout)
    ci, ci_url, available = macro._leading_ci(market['leading_ci_page_url'], as_of=as_of, timeout=timeout, cutoff_at=cutoff)
    return {'schema_version':'1.0','as_of':as_of.isoformat(),'information_cutoff_at_jst':cutoff.isoformat(),
            **{k:market[k] for k in ('minimum_vi_observations','minimum_breadth_coverage','breadth_lookback_sessions','maximum_breadth_close_age_days')},
            'series':{**indices, 'nikkei_vi':{'source_url':vi_url,'primary_source':True,'points':vi},
                      'leading_ci':{'source_url':ci_url,'primary_source':True,'available_at_jst':available,'points':ci}}}
