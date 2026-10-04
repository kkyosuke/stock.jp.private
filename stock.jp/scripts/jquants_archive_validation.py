"""Read-only validation of every retained J-Quants partition and referenced page."""
from datetime import date, datetime
import hashlib
import json
import re

try:
    from scripts import jquants_calendar as calendar_validation
    from scripts.jquants_catalog import BASE_URL, BY_NAME, query_for
except ModuleNotFoundError:
    import jquants_calendar as calendar_validation
    from jquants_catalog import BASE_URL, BY_NAME, query_for


def require(condition):
    if not condition:
        raise ValueError('invalid retained J-Quants revision or page')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def timestamp(value):
    require(datetime.fromisoformat(value).tzinfo is not None)


def validate_archive(names, read):
    """Names are relative to market-data; read must return exact member bytes.

    Use a callback so the codec can validate references without holding the raw
    archive in memory. Valid compressed checksums alone do not prove that the
    provider page hashes embedded in historical manifests still match.
    """
    names = set(names)
    history = "jquants/history/state.json"
    if "jquants/history-marker.json" in names or any(name.startswith("jquants/history/") for name in names):
        require(history in names)
        try:
            from scripts.jquants_history import validate_history
        except ModuleNotFoundError:
            from jquants_history import validate_history
        state = json.loads(read(history))
        validate_history(state, read)
        if state["status"] == "COMPLETE":
            require(not any(f"jquants/datasets/{dataset}/{day}/checkpoint.json" in names
                            for day in state["days"] for dataset in ("equities_master", "equities_daily")))
        marker = "jquants/history-marker.json"
        if any(state["batches"]):
            require(marker in names)
        if marker in names:
            require(json.loads(read(marker)) == {"as_of": state["as_of"], "sessions": state["sessions"]})
    shared_name = 'jquants/target-history/ranges.json'
    if shared_name in names:
        shared = json.loads(read(shared_name))
        require(set(shared) == {'campaign', 'ranges', 'knowledge'}
                and shared['knowledge'] == 'retrieved_now_not_point_in_time')
        campaign = shared['campaign']
        require(set(campaign) == {'start_date', 'as_of', 'sessions'})
        start, end = date.fromisoformat(campaign['start_date']), date.fromisoformat(campaign['as_of'])
        require(0 <= (end-start).days <= 550 and type(campaign['sessions']) is int
                and 1 <= campaign['sessions'] <= 280)
        require(set(shared['ranges']) <= {'trading_calendar', 'topix_daily'})
        for dataset, value in shared['ranges'].items():
            require(set(value) == {'query', 'pages', 'next_key', 'seen_keys', 'complete'}
                    and type(value['complete']) is bool and isinstance(value['pages'], list)
                    and isinstance(value['seen_keys'], list))
            require(value['query'] == {'from':campaign['start_date'], 'to':campaign['as_of']})
            seen = []
            collected_rows = []
            last = None
            for index, page in enumerate(value['pages']):
                require(set(page) == {'path', 'sha256', 'bytes', 'retrieved_at'}
                        and isinstance(page['sha256'], str) and re.fullmatch('[0-9a-f]{64}', page['sha256'])
                        and type(page['bytes']) is int and page['bytes'] >= 0)
                member = 'jquants/' + page['path']
                require(page['path'] == 'objects/' + page['sha256'][:2] + '/' + page['sha256'] + '.json'
                        and member in names and sha(read(member)) == page['sha256']
                        and len(read(member)) == page['bytes'])
                timestamp(page['retrieved_at'])
                payload = json.loads(read(member))
                require(isinstance(payload.get('data'), list) and all(isinstance(v, dict) for v in payload['data']))
                collected_rows.extend(payload['data'])
                last = payload.get('pagination_key') or None
                require(last is None or isinstance(last, str))
                require(bool(last) or index == len(value['pages'])-1)
                if last:
                    require(last not in seen)
                    seen.append(last)
            require(value['seen_keys'] == seen and value['next_key'] == last
                    and value['complete'] == (bool(value['pages']) and last is None))
            if dataset == 'trading_calendar':
                calendar_validation.validate(collected_rows, campaign['start_date'], campaign['as_of'],
                    complete=value['complete'], sessions=campaign['sessions'])
    for name in sorted(names):
        if re.fullmatch(r'jquants/runs/[0-9]{8}T120000Z\.json', name):
            journal = json.loads(read(name))
            require(journal.get('daily_runner_version') == 1 and isinstance(journal.get('tasks'), dict))
            for task, receipt in journal['tasks'].items():
                dataset, day_text = task.split(':')
                require(dataset in BY_NAME and date.fromisoformat(day_text).isoformat() == day_text)
                digest = receipt['manifest_sha256']
                require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest) is not None)
                revision = f'jquants/datasets/{dataset}/{day_text}/revisions/{digest}.json'
                require(revision in names and sha(read(revision)) == digest)
                require(json.loads(read(revision))['row_count'] == receipt['rows'])
    for name in sorted(names):
        if not name.startswith('jquants/datasets/'):
            continue
        match = re.fullmatch(r'jquants/datasets/([a-z_]+)/([0-9-]{10})/(latest\.json|revisions/([0-9a-f]{64})\.json)', name)
        if not match:
            # Checkpoints are validated by the collector; not immutable revisions.
            require('/revisions/' not in name)
            continue
        dataset, day_text, suffix, revision_sha = match.groups()
        require(dataset in BY_NAME)
        day = date.fromisoformat(day_text)
        data = read(name)
        if revision_sha:
            require(sha(data) == revision_sha)
        else:
            revision = name.removesuffix('latest.json') + 'revisions/' + sha(data) + '.json'
            require(revision in names and read(revision) == data)
        manifest = json.loads(data)
        require(manifest['schema_version'] == 1 and manifest['api_version'] == 'v2'
                and manifest['provider'] == 'J-Quants API V2' and manifest['dataset'] == dataset
                and manifest['endpoint'] == BASE_URL + BY_NAME[dataset].endpoint
                and manifest['query'] == query_for(BY_NAME[dataset], day)
                and manifest['period'] == {'from': day_text, 'to': day_text}
                and manifest['status'] == 'complete')
        timestamp(manifest['started_at']); timestamp(manifest['completed_at'])
        pages = manifest['pages']
        require(isinstance(pages, list) and bool(pages))
        total = 0
        seen = set()
        for index, page in enumerate(pages):
            path = page['path']; digest = page['sha256']
            require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest) is not None)
            require(path == f'objects/{digest[:2]}/{digest}.json')
            member = 'jquants/' + path
            require(member in names)
            blob = read(member)
            require(sha(blob) == digest and type(page['bytes']) is int and len(blob) == page['bytes'])
            payload = json.loads(blob)
            rows = payload['data']
            require(isinstance(rows, list) and all(isinstance(r, dict) for r in rows))
            require(type(page['row_count']) is int and page['row_count'] == len(rows)
                    and page['page_number'] == index + 1)
            timestamp(page['retrieved_at'])
            token = payload.get('pagination_key')
            require(token is None or isinstance(token, str))
            require(bool(token) == (index < len(pages) - 1))
            if token:
                require(token not in seen)
                seen.add(token)
            total += len(rows)
        require(type(manifest['row_count']) is int and manifest['row_count'] == total)
    return True
