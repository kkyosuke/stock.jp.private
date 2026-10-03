from datetime import date, datetime, timedelta
import json
import copy
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.jquants_ingest import ApiError, IngestionError, RequestBudgetExhausted
from scripts.nightly_jquants_cache import NightlyClient
from scripts.non_edinet_sources import NonEdinetSourceError, _jquants_pages, collect_non_edinet_sources
from scripts.operation_state import PROJECT_ROOT, initialize_or_migrate_workspace
from scripts.official_source_scan import _gap, _merge_gaps, _resolve_gaps
from scripts.run_integrity import _validate_research_artifacts


class NightlyCacheTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cutoff = '2026-10-02T21:00:00+09:00'
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def client(self, transport, **kwargs):
        return NightlyClient(private=self.root, cutoff=kwargs.pop('cutoff', self.cutoff),
                             retrieved=kwargs.pop('retrieved', self.cutoff), plan='light',
                             transport=transport, sleep=lambda _: None, **kwargs)

    def pages(self, client):
        return _jquants_pages(base_url='https://api.jquants.com/v2', path='fins/summary',
                             params={'date': '2026-10-02'}, client=client, timeout=30)

    def test_interrupted_pagination_restarts_across_midnight(self):
        def transport(endpoint, params):
            self.calls.append(params.copy())
            page = int(params.get('pagination_key', '0'))
            return {'data': [{'page': page}], **({'pagination_key': str(page + 1)} if page < 2 else {})}
        with self.assertRaises(NonEdinetSourceError):
            self.pages(self.client(transport, budget=2))
        rows, _ = self.pages(self.client(transport, retrieved='2026-10-03T00:01:00+09:00'))
        self.assertEqual(rows, [{'page': 0}, {'page': 1}, {'page': 2}])
        self.assertEqual(len(self.calls), 3)
        replay = self.client(transport)
        self.assertEqual(self.pages(replay)[0], rows)
        self.assertEqual((replay.requests, replay.cache_hits, len(self.calls)), (0, 3, 3))

    def test_changed_cutoff_or_query_does_not_reuse_old_pages(self):
        def transport(endpoint, params):
            self.calls.append(params.copy())
            return {'data': []}
        first = self.client(transport)
        first.get('/fins/summary', {'date': '2026-10-02'})
        self.client(transport, cutoff='2026-10-03T21:00:00+09:00').get('/fins/summary', {'date': '2026-10-02'})
        first.get('/fins/summary', {'date': '2026-10-01'})
        self.assertEqual(len(self.calls), 3)

    def test_retries_consume_budget_and_invalid_budget_blocks(self):
        def transport(*_):
            self.calls.append(True)
            raise ApiError(503, 'temporary')
        client = self.client(transport, budget=2)
        with self.assertRaises(RequestBudgetExhausted):
            client.get('/fins/summary', {'date': '2026-10-02'})
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(list(self.root.rglob('pages/*.json')), [])
        for budget in (None, 0, 13, True, '12'):
            with self.assertRaises(IngestionError):
                self.client(transport, budget=budget)

    def test_corrupt_checkpoint_and_page_fail_closed(self):
        client = self.client(lambda *_: {'data': []})
        client.get('/fins/summary', {})
        checkpoint = next(client.root.glob('pages/*.json'))
        receipt = json.loads(checkpoint.read_text())
        (client.root / receipt['object']['path']).write_text('{"data":[{"changed":true}]}')
        with self.assertRaises(IngestionError):
            client.get('/fins/summary', {})
        checkpoint.write_text('{}')
        with self.assertRaises(IngestionError):
            client.get('/fins/summary', {})
        self.assertEqual(client.requests, 1)

    def test_null_receipt_and_invalid_retrieval_times_never_refetch(self):
        client = self.client(lambda *_: {'data': []})
        client.get('/fins/summary', {})
        checkpoint = next(client.root.glob('pages/*.json'))
        receipt = json.loads(checkpoint.read_text())
        for bad in (None, {**receipt, 'retrieved_at_jst': 'not-a-date'},
                    {**receipt, 'retrieved_at_jst': '2026-10-02T21:00:00'}):
            with self.subTest(receipt=bad):
                checkpoint.write_text(json.dumps(bad))
                before = checkpoint.read_bytes()
                with self.assertRaises(IngestionError):
                    client.get('/fins/summary', {})
                self.assertEqual(client.requests, 1)
                self.assertEqual(checkpoint.read_bytes(), before)

    def test_provider_error_blocks_completion_even_without_open_gaps(self):
        for status in ('ERROR', 'INCOMPLETE', 'MISSING_CREDENTIAL', 'UNKNOWN'):
            errors = _validate_research_artifacts(
                health={'run_id': '2026-10-02', 'status': 'COMPLETED',
                        'started_at_jst': self.cutoff, 'completed_at_jst': self.cutoff,
                        'providers': {'jquants_financials': {'status': status}}},
                queue={'run_id': '2026-10-02', 'generated_at_jst': self.cutoff, 'tasks': []},
                run_id='2026-10-02', handoff={'pending_reviews': []}, valid_source_ids=set())
            self.assertTrue(any('incomplete query coverage' in error for error in errors))

    def test_manual_reminder_does_not_erase_prior_verified_resolution(self):
        gap = _gap(gap_id='manual-ir', source='company_ir', impact='check IR', retry_after=self.cutoff)
        container = {'data_gaps': [copy.deepcopy(gap)]}
        _resolve_gaps(container, sources={'company_ir'}, resolved_at=self.cutoff)
        before = copy.deepcopy(container)
        _merge_gaps(container, [gap], failed_sources={'nightly_jquants_scan'})
        self.assertEqual(container, before)

    def test_missing_credentials_block_enabled_but_not_disabled_datasets(self):
        private = self.root / 'private'
        config = {'jpx_listed_master': {'enabled': False}, 'jquants': {
            'enabled': True, 'api_key_env': 'JQUANTS_API_KEY', 'plan': 'light',
            'listed_master_enabled': False, 'daily_bars_enabled': False,
            'financial_summary_enabled': True, 'earnings_calendar_enabled': True,
            'trading_calendar_enabled': False}}
        args = dict(run_id='2026-10-02', scan_dates=[date(2026, 10, 2)], targets=set(),
                    cutoff_at=datetime.fromisoformat(self.cutoff), started=datetime.fromisoformat(self.cutoff),
                    config=config, run_dir=private / 'runs/2026-10-02', private=private, environ={})
        result = collect_non_edinet_sources(**args)
        self.assertEqual(result['providers']['jquants_security_master']['status'], 'DISABLED')
        self.assertEqual(result['providers']['jquants_financials']['status'], 'MISSING_CREDENTIAL')
        self.assertEqual(result['providers']['jquants_earnings_calendar']['status'], 'MISSING_CREDENTIAL')
        self.assertIn('nightly_jquants_scan', [g['source'] for g in result['blocking_failures']])
        # Enable only an empty master query; optional disabled datasets must not
        # prevent successful enabled-query aggregation or cause any API calls.
        config['jquants'].update(listed_master_enabled=True, financial_summary_enabled=False,
                                  earnings_calendar_enabled=False)
        fixture = self.root / 'fixtures'
        fixture.mkdir()
        (fixture / 'jquants-master-2026-10-02.json').write_text('{"data":[]}')
        result = collect_non_edinet_sources(**args, fixture_dir=fixture)
        self.assertIn('nightly_jquants_scan', result['successful_gap_sources'])
        self.assertEqual(result['blocking_failures'], [])

    def test_invalid_response_and_pagination_cycle_never_complete(self):
        bad = self.client(lambda *_: {'data': 'invalid'})
        with self.assertRaises(IngestionError):
            bad.get('/fins/summary', {})
        self.assertEqual(list(bad.root.glob('pages/*.json')), [])
        cycle = self.client(lambda *_: {'data': [], 'pagination_key': 'same'})
        with self.assertRaises(NonEdinetSourceError):
            self.pages(cycle)
        replay = self.client(lambda *_: self.fail('must reuse cached pages'))
        with self.assertRaises(NonEdinetSourceError):
            self.pages(replay)

    def test_atomic_checkpoint_interruption_does_not_advance(self):
        client = self.client(lambda *_: {'data': []})
        with patch('scripts.nightly_jquants_cache.atomic_json', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                client.get('/fins/summary', {})
        self.assertEqual(list(client.root.glob('pages/*.json')), [])
        client.get('/fins/summary', {})
        self.assertEqual(client.requests, 2)

    def test_19_day_backlog_bounded_resume_and_idempotent_replay(self):
        shutil.copytree(PROJECT_ROOT / 'operations/templates', self.root / 'operations/templates')
        initialize_or_migrate_workspace(self.root)
        private = self.root / 'operations/private'
        config = json.loads((private / 'source-config.json').read_text())
        config['jquants']['plan'] = 'light'
        config['jpx_listed_master']['enabled'] = False
        config['jquants']['minimum_liquidity_sessions'] = 20
        run_dir = private / 'runs/2026-10-02'
        cutoff = datetime.fromisoformat(self.cutoff)
        def transport(endpoint, params):
            self.calls.append((endpoint, params.copy()))
            if endpoint == '/markets/calendar':
                start=date.fromisoformat(params['from']);end=date.fromisoformat(params['to'])
                return {'data': [{'Date': str(start+timedelta(days=i)), 'HolDiv': '1' if (start+timedelta(days=i)).weekday()<5 else '0'} for i in range((end-start).days+1)]}
            if endpoint == '/equities/bars/daily':
                return {'data': [{'Date': str(date(2026,10,2)-timedelta(days=i)), 'Code': '12340', 'O': 10, 'H': 10,
                                  'L': 10, 'C': 10, 'Vo': 100, 'Va': 1000, 'AdjFactor': 1} for i in range(40) if (date(2026,10,2)-timedelta(days=i)).weekday()<5]}
            return {'data': []}
        clients = []
        def factory(**kwargs):
            client = NightlyClient(**kwargs, transport=transport, sleep=lambda _: None)
            clients.append(client)
            return client
        args = dict(run_id='2026-10-02', scan_dates=[date(2026, 9, 14) + timedelta(days=i) for i in range(19)],
                    targets={'1234'}, cutoff_at=cutoff, started=cutoff, config=config,
                    run_dir=run_dir, private=private, environ={'JQUANTS_API_KEY': 'synthetic-test'})
        before = (private / 'state.json').read_bytes()
        containers = [{'data_gaps': []}, {'data_gaps': []}]
        def merge_result(result):
            failed = {failure['source'] for failure in result['blocking_failures']}
            gaps = [_gap(gap_id='2026-10-02-' + source, source=source,
                         impact='synthetic failure', retry_after=self.cutoff) for source in failed]
            for container in containers:
                _resolve_gaps(container, sources=result['successful_gap_sources'], resolved_at=self.cutoff)
                _merge_gaps(container, gaps, failed_sources=failed)
        with patch('scripts.nightly_jquants_cache.NightlyClient', side_effect=factory):
            for index in range(4):
                result = collect_non_edinet_sources(**args)
                merge_result(result)
                self.assertLessEqual(clients[-1].requests, 12)
                self.assertEqual(sum(p['request_count'] for p in result['providers'].values()), clients[-1].requests)
                if index < 3:
                    self.assertIn('nightly_jquants_scan', [g['source'] for g in result['blocking_failures']])
                    self.assertNotIn('nightly_jquants_scan', result['successful_gap_sources'])
            self.assertIn('nightly_jquants_scan', result['successful_gap_sources'])
            self.assertEqual([c.requests for c in clients], [12, 12, 12, 5])
            self.assertEqual(len(self.calls), 41)
            self.assertEqual(len({json.dumps(c, sort_keys=True) for c in self.calls}), 41)
            files = {p: p.read_bytes() for p in (private / 'corporate-actions.csv', private / 'earnings-calendar-history.csv')}
            replay = collect_non_edinet_sources(**args)
            self.assertEqual(clients[-1].requests, 0)
            self.assertEqual(result['source_rows'], replay['source_rows'])
            for path, content in files.items():
                self.assertEqual(path.read_bytes(), content)
            resolved = copy.deepcopy(containers[0]['data_gaps'][0])
            self.assertEqual(resolved['status'], 'RESOLVED')
            obj = next(clients[-1].root.glob('objects/*/*.json'))
            obj.write_text('{"data":[{"corrupted":true}]}')
            broken = collect_non_edinet_sources(**args)
            self.assertEqual(clients[-1].requests, 0)
            merge_result(broken)
            merge_result(broken)  # repeated failure must not erase/duplicate history
            for container in containers:
                reopened = container['data_gaps'][0]
                self.assertEqual(reopened['status'], 'OPEN')
                self.assertIsNone(reopened['resolved_at_jst'])
                self.assertEqual(reopened['resolution_history'], [resolved])
        self.assertEqual((private / 'state.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
