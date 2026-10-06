"""One explicit index bootstrap, charged to the owning daily run's 12-call ledger."""
from datetime import date, datetime
import json
import math
import re

from stock_jp.market_data.jquants import ingest

PATH = 'index-history/growth250.json'
MEMBER = 'jquants/' + PATH
ENDPOINT = '/indices/bars/daily'


def require(value, message='invalid Growth250 bootstrap evidence'):
    if not value:
        raise ingest.IngestionError(message)


def valid_rows(rows, *, start, through):
    require(isinstance(rows, list))
    seen = set()
    for row in rows:
        require(isinstance(row, dict) and row.get('Code') == '0070')
        day = date.fromisoformat(row['Date'])
        require(start <= day <= through and day not in seen)
        seen.add(day)
        value = row.get('C')
        require(type(value) in (int, float) and math.isfinite(value) and value > 0)
    return rows


def validate(document, read):
    require(set(document) == {'version', 'query', 'run_date', 'max_requests', 'attempts',
                             'pages', 'next_key', 'seen_keys', 'complete', 'started_at'})
    query = document['query']
    require(set(query) == {'code', 'from', 'to'} and query['code'] == '0070')
    start, end = date.fromisoformat(query['from']), date.fromisoformat(query['to'])
    require(start <= end < date.fromisoformat(document['run_date']) and (end-start).days <= 550)
    require(document['version'] == 1 and type(document['max_requests']) is int
            and 1 <= document['max_requests'] <= 3)
    require(datetime.fromisoformat(document['started_at']).tzinfo is not None)
    require(isinstance(document['attempts'], list) and len(document['attempts']) <= document['max_requests'])
    for attempt in document['attempts']:
        require(set(attempt) == {'query_sha256', 'reserved_at'})
        require(re.fullmatch('[0-9a-f]{64}', attempt['query_sha256']) is not None)
        require(datetime.fromisoformat(attempt['reserved_at']).tzinfo is not None)
    rows, keys = [], []
    require(isinstance(document['pages'], list) and type(document['complete']) is bool)
    for index, page in enumerate(document['pages']):
        digest = page['sha256']
        require(re.fullmatch('[0-9a-f]{64}', digest) is not None)
        require(page['path'] == 'objects/' + digest[:2] + '/' + digest + '.json')
        raw = read('jquants/' + page['path'])
        require(ingest.digest(raw) == digest and len(raw) == page['bytes'])
        require(datetime.fromisoformat(page['retrieved_at']).tzinfo is not None)
        payload = json.loads(raw)
        require(page['row_count'] == len(payload['data']))
        rows.extend(payload['data'])
        token = payload.get('pagination_key') or None
        require(token is None or isinstance(token, str))
        if token:
            require(token not in keys)
            keys.append(token)
        else:
            require(index == len(document['pages']) - 1)
    valid_rows(rows, start=start, through=end)
    last = json.loads(read('jquants/' + document['pages'][-1]['path'])).get('pagination_key') or None if document['pages'] else None
    require(document['seen_keys'] == keys and document['next_key'] == last)
    require(document['complete'] == (bool(document['pages']) and last is None))
    require(len(document['pages']) <= len(document['attempts']))
    if document['complete']:
        require(len(rows) >= 200, 'Growth250 bootstrap has fewer than 200 sessions')
    return rows


def collect(archive, client, *, day, approval, persist):
    """Never borrows historical campaign allowance or resets an old attempt list."""
    require(set(approval) == {'from', 'through', 'max_requests', 'run_date'})
    require(approval['run_date'] == day.isoformat(), 'bootstrap belongs to a different daily run')
    path = archive / PATH
    document = ingest.read_json(path)
    query = {'code': '0070', 'from': approval['from'], 'to': approval['through']}
    if document is None:
        document = {'version': 1, 'query': query, 'run_date': day.isoformat(),
                    'max_requests': approval['max_requests'], 'attempts': [], 'pages': [],
                    'next_key': None, 'seen_keys': [], 'complete': False, 'started_at': ingest.utc_now()}
    require(document['query'] == query and document['run_date'] == day.isoformat()
            and document['max_requests'] == approval['max_requests'], 'bootstrap contract changed')
    read = lambda name: (archive.parent / name).read_bytes()
    validate(document, read)
    while not document['complete']:
        if len(document['attempts']) >= document['max_requests']:
            raise ingest.RequestBudgetExhausted('approved index bootstrap exhausted')
        params = dict(query)
        if document['next_key']:
            params['pagination_key'] = document['next_key']
        document['attempts'].append({'query_sha256': ingest.digest(ingest.encoded(params)),
                                     'reserved_at': ingest.utc_now()})
        ingest.atomic_json(path, document)
        persist()  # index allowance is durable before the daily client's reservation/I/O
        payload = client.get(ENDPOINT, params)
        rows = payload.get('data')
        valid_rows(rows, start=date.fromisoformat(query['from']), through=date.fromisoformat(query['to']))
        token = payload.get('pagination_key') or None
        require(token is None or isinstance(token, str))
        require(not token or token not in document['seen_keys'], 'index pagination cycle')
        ref = ingest.save_object(archive, payload)
        ref.update(retrieved_at=ingest.utc_now(), row_count=len(rows))
        document['pages'].append(ref)
        document['next_key'] = token
        if token:
            document['seen_keys'].append(token)
        document['complete'] = token is None
        # Persist the received page even if the final coverage/duplicate check fails.
        ingest.atomic_json(path, document)
        persist()
        validate(document, read)
    return document
