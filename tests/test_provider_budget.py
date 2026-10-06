import copy
from datetime import datetime
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.request import HTTPSHandler, build_opener

from stock_jp.automation import nightly as n
from stock_jp.automation.provider_budget import PaperBudget, guard
from stock_jp.market_data.jquants import ingest
from stock_jp.market_data.jquants.cache import NightlyClient
from tests.test_nightly_market_data import Repository


class OfflineHTTPS(HTTPSHandler):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def https_open(self, request):
        self.callback(request)
        response = io.BytesIO(b'{"data":[]}')
        response.code = 200
        response.msg = "OK"
        response.status = 200
        response.url = request.full_url
        response.headers = {}
        return response


class ProviderBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.at = datetime.fromisoformat('2026-10-07T21:00:00+09:00')
        self.day = self.at.date().isoformat()
        ingest.atomic_json(self.root / 'config/policy.json', {'operation_mode': 'PAPER', 'broker_submission': 'HUMAN_ONLY'})
        ingest.atomic_json(self.root / 'config/sources.json', {'market_data_git': {'automatic_upload': True},
            'jquants': {'ingestion': {'max_requests_per_run': 12}}})
        self.repo = Repository(self.root)
        self.context = {'repository': n.REPO, 'event': 'schedule', 'ref': 'refs/heads/main',
            'sha': self.repo.refs['main'], 'actor': 'kkyosuke', 'triggering_actor': 'kkyosuke', 'run_id': 123}
        self.seed(10)

    def seed(self, count):
        state = {'daily_runner_version': 1, 'run_date': self.day, 'started_at': self.at.isoformat(),
            'config_sha256': ingest.digest((self.root / 'config/sources.json').read_bytes()), 'budget': 12,
            'requests': [{'endpoint': '/markets/calendar', 'query_sha256': 'a' * 64,
                'reserved_at': self.at.isoformat()} for _ in range(count)], 'tasks': {},
            'status': 'PAUSED', 'data_date': None, 'error': None, 'http_status': None, 'retry_not_before': None}
        doc = {'version': 1, 'runs': {self.day: state}, 'files': {}, 'github_budget': None}
        tree = self.repo.tree(self.repo.refs['main'])
        tree[n.JOURNAL] = self.repo.blob(ingest.encoded(doc))
        self.repo.refs[n.CHECKPOINT] = self.repo.commit(tree)

    def budget(self, **kwargs):
        return PaperBudget(self.root, self.context, n.Git(self.repo), clock=lambda: self.at, **kwargs)

    def test_restart_and_later_schedule_share_remaining_two_including_failures(self):
        seen = []
        def fail(request):
            seen.append(request.host)
            # Durable receipt exists before the (mocked) network is entered.
            self.assertEqual(len(self.repo.journal()['paper_requests'][self.day]), len(seen))
            raise TimeoutError('offline transport failed')
        opener = build_opener(OfflineHTTPS(fail))
        with guard(self.budget()), self.assertRaises(TimeoutError):
            opener.open('https://api.jquants.com/v2/equities/master?secret=never-store')
        self.at = self.at.replace(minute=20)
        self.context['run_id'] = 124
        with guard(self.budget()), self.assertRaises(TimeoutError):
            opener.open('https://api.edinet-fsa.go.jp/api/v2/documents.json')
        with guard(self.budget()), self.assertRaises(ingest.RequestBudgetExhausted):
            opener.open('https://example.com/primary.pdf')
        self.assertEqual(len(seen), 2)
        self.assertNotIn('never-store', json.dumps(self.repo.journal()))

    def test_eleven_daily_requests_leave_one_and_reservation_survives_crash(self):
        self.seed(11)
        first = self.budget()
        with patch.object(first.checkpoint, 'before_provider_io', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                first.reserve('https://www.jpx.co.jp/primary.xlsx')
        self.assertEqual(len(self.repo.journal()['paper_requests'][self.day]), 1)
        with self.assertRaises(ingest.RequestBudgetExhausted):
            self.budget().reserve('https://www.jpx.co.jp/primary.xlsx')

    def test_git_write_failure_prevents_network_and_poisons_session(self):
        seen = []
        budget = self.budget()
        self.repo.fail_patch = True
        with guard(budget), self.assertRaises(n.RemoteStop):
            build_opener(OfflineHTTPS(seen.append)).open('https://example.com/source')
        self.repo.fail_patch = False
        with self.assertRaises(n.RemoteStop):
            budget.reserve('https://example.com/source')
        self.assertFalse(seen)

    def test_cache_replay_spends_nothing_even_when_allowance_exhausted(self):
        cutoff = self.at.isoformat()
        cached = NightlyClient(private=self.root / 'operations', cutoff=cutoff, retrieved=cutoff,
            plan='standard', transport=lambda *_: {'data': []})
        cached.get('/equities/master', {})
        self.seed(12)
        with guard(self.budget()):
            replay = NightlyClient(private=self.root / 'operations', cutoff=cutoff, retrieved=cutoff,
                plan='standard', transport=lambda *_: self.fail('cache miss'))
            self.assertEqual(replay.get('/equities/master', {}), {'data': []})
        self.assertEqual(replay.requests, 0)
        self.assertEqual(self.repo.journal()['paper_requests'][self.day], [])

    def test_market_collector_subtracts_paper_reservations(self):
        budget = self.budget()
        budget.reserve('https://example.com/one')
        budget.reserve('https://example.com/two')
        cp = n.Checkpoint(self.root, n.Git(self.repo), self.repo.refs['main'], self.at.date(), self.context)
        cp.state = copy.deepcopy(cp.document['runs'][self.day])
        cp.old_charged, cp.baseline_requests = 10, 10
        with self.assertRaises(ingest.RequestBudgetExhausted):
            cp.before_reservation()

    def test_actual_jquants_retry_loop_cannot_exceed_remaining_two(self):
        seen = []
        def fail(request):
            seen.append(request.host)
            raise TimeoutError('fixture')
        with patch.dict('os.environ', {'JQUANTS_API_KEY': 'synthetic'}):
            client = ingest.Client(plan='standard', max_requests=12, sleep=lambda _: None)
        client.opener = build_opener(OfflineHTTPS(fail))
        with guard(self.budget()), self.assertRaises(ingest.RequestBudgetExhausted):
            client.get('/equities/master', {})
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(self.repo.journal()['paper_requests'][self.day]), 2)

    def test_binding_survives_next_publication_window_and_cutoff_changes(self):
        first = self.budget()
        first.reserve('https://example.com/source?cutoff=first')
        self.at = self.at.replace(day=8, minute=20)
        second = self.budget()
        self.assertEqual(second.day, self.day)
        second.reserve('https://example.com/source?cutoff=different')
        with self.assertRaises(ingest.RequestBudgetExhausted):
            second.reserve('https://example.com/source')

    def test_legacy_same_day_inbox_does_not_reset_unknown_consumption(self):
        tree = self.repo.tree(self.repo.refs['main'])
        tree[n.PAPER_JOURNAL] = self.repo.blob(ingest.encoded({
            'day': self.day, 'captured_at_jst': self.at.isoformat()}))
        self.repo.refs[n.PAPER_CHECKPOINT] = self.repo.commit(tree)
        with self.assertRaisesRegex(n.RemoteStop, 'legacy PAPER traffic'):
            self.budget()
        self.assertNotIn('paper_requests', self.repo.journal())

    def test_exhausted_public_collector_preserves_blocker_without_network(self):
        from stock_jp.market_data.primary_sources import collect, Download
        config_path = self.root / 'config/sources.json'
        config = json.loads(config_path.read_text())
        config['market_regime'] = {'fixture_url': 'https://example.com/primary.csv'}
        ingest.atomic_json(config_path, config)
        self.seed(12)
        ingest.atomic_json(self.root / 'operations/inputs/primary-sources.json', {
            'allowed_hosts': ['example.com'], 'max_requests': 60, 'issuers': [],
            'public_series': [{'id': 'fixture', 'title': 'Fixture', 'config_key': 'fixture_url', 'suffix': '.csv'}]})
        seen = []
        download = Download(['example.com'], opener=build_opener(OfflineHTTPS(seen.append)).open)
        with guard(self.budget()):
            result = collect(self.root, cutoff=self.at, download=download)
        self.assertEqual(result['sources'], [])
        self.assertEqual(result['blockers'][0]['source'], 'fixture')
        self.assertIn('allowance exhausted', result['blockers'][0]['error'])
        self.assertFalse(seen)

    def test_prior_grant_missing_journal_and_stale_writer_fail_closed(self):
        ingest.atomic_json(self.root / 'operations/acquisition/date-grants.json', {'prior_charges': {self.day: 12}})
        with self.assertRaises(ingest.RequestBudgetExhausted):
            self.budget().reserve('https://example.com/source')
        ingest.atomic_json(self.root / 'operations/acquisition/date-grants.json', {})
        stale = self.budget()
        self.budget().reserve('https://example.com/source')
        with self.assertRaises(n.RemoteStop):
            stale.reserve('https://example.com/source')
        del self.repo.refs[n.CHECKPOINT]
        with self.assertRaises(n.RemoteStop):
            self.budget()

    def test_workflow_uses_guard_for_both_provider_steps_without_permission_expansion(self):
        text = (Path(__file__).parents[1] / '.github/workflows/market-data-manual.yml').read_text()
        self.assertIn('stock_jp.automation.provider_budget forward', text)
        self.assertIn('stock_jp.automation.provider_budget prepare', text)
        self.assertNotIn('-m stock_jp.operation.paper_preparation', text)
        self.assertNotIn('-m stock_jp.research.forward_collection', text)
