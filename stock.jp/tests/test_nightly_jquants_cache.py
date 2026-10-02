from datetime import date, datetime, timedelta
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.jquants_ingest import ApiError, IngestionError, RequestBudgetExhausted
from scripts.nightly_jquants_cache import NightlyClient
from scripts.non_edinet_sources import NonEdinetSourceError, _jquants_pages, collect_non_edinet_sources
from scripts.operation_state import PROJECT_ROOT, initialize_or_migrate_workspace


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
        config['jquants']['minimum_liquidity_sessions'] = 1
        run_dir = private / 'runs/2026-10-02'
        cutoff = datetime.fromisoformat(self.cutoff)
        def transport(endpoint, params):
            self.calls.append((endpoint, params.copy()))
            if endpoint == '/markets/calendar':
                return {'data': [{'Date': '2026-10-05', 'HolDiv': '1'}]}
            if endpoint == '/equities/bars/daily':
                return {'data': [{'Date': '2026-10-02', 'Code': '12340', 'O': 10, 'H': 10,
                                  'L': 10, 'C': 10, 'Vo': 100, 'Va': 1000, 'AdjFactor': 1}]}
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
        with patch('scripts.nightly_jquants_cache.NightlyClient', side_effect=factory):
            for index in range(4):
                result = collect_non_edinet_sources(**args)
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
        self.assertEqual((private / 'state.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
