"""Read-only validation of every retained J-Quants partition and referenced page."""
from datetime import date, datetime
import hashlib
import json
import re

try:
    from scripts.jquants_catalog import BASE_URL, BY_NAME, query_for
except ModuleNotFoundError:
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
