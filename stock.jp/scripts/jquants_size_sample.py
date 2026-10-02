"""Bounded Light dataset sample; aggregate sizes only, no raw records on stdout."""
from datetime import date, datetime
import gzip
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from jquants_catalog import CATALOG, entitlement, period
from jquants_ingest import (ApiError, Client, IngestionError, RequestBudgetExhausted,
                            archive_lock, atomic_json, collect_partition, private_root)


def main():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / 'operations/private/source-config.json').read_text())
    settings = config['jquants']
    budget = settings['ingestion']['max_requests_per_run']
    if settings['plan'] != 'light' or settings.get('addons') or type(budget) is not int or not 1 <= budget <= 60:
        raise IngestionError('Light sample requires a finite 1-60 request budget and no add-ons')
    archive = private_root(root, settings['ingestion']['archive_root'])
    latest = json.loads((archive / 'latest.json').read_text())
    day = date.fromisoformat(latest.get('daily_snapshot', {}).get('date', datetime.now(ZoneInfo('Asia/Tokyo')).date().isoformat()))
    client = Client(plan='light', max_requests=budget, retries=0)
    result = {'sample_date': day.isoformat(), 'all_entitled_complete': False, 'datasets': {},
              'estimate_basis': 'one date per dataset; not an upper bound; empty or unavailable samples remain unknown'}
    with archive_lock(archive):
        for dataset in CATALOG:
            if dataset.role != 'dataset' or not entitlement(dataset, 'light'):
                continue
            entry = {}
            try:
                partition = collect_partition(archive, dataset, day, client)
                compressed = raw = 0
                for page in partition['pages']:
                    blob = (archive / page['path']).read_bytes()
                    raw += len(blob)
                    compressed += len(gzip.compress(blob, mtime=0))
                start, end = period(dataset, 'light', day)
                partitions = (end - start).days + 1
                entry = {'status': 'empty' if partition['row_count'] == 0 else 'sampled',
                         'rows': partition['row_count'], 'raw_bytes': raw, 'compressed_bytes': compressed,
                         'requested_partitions': partitions,
                         'linear_estimate_bytes': compressed * partitions if partition['row_count'] else None}
            except RequestBudgetExhausted:
                entry = {'status': 'pending_resume'}
            except ApiError as error:
                entry = {'status': 'not_available' if error.status == 210 else 'error', 'http_status': error.status, 'error': error.kind}
            except (IngestionError, ValueError, OSError, KeyError, TypeError):
                entry = {'status': 'error', 'error': 'local_sample_validation'}
            result['datasets'][dataset.name] = entry
        result['requests_this_run'] = client.requests
        estimates = [x.get('linear_estimate_bytes') for x in result['datasets'].values()]
        result['known_component_estimate_bytes'] = sum(x for x in estimates if x is not None)
        result['all_datasets_have_nonempty_samples'] = all(x is not None for x in estimates)
        result['conservative_store_budget_bytes'] = 750 * 1024 * 1024
        result['pause_full_backfill_for_sizing'] = (not result['all_datasets_have_nonempty_samples']
            or result['known_component_estimate_bytes'] > result['conservative_store_budget_bytes'])
        atomic_json(archive / 'size-sample.json', result)
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'status': 'blocked', 'error': 'sample precondition or local validation failure'}))
        raise SystemExit(2)
