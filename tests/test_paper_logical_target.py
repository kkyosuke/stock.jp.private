from datetime import datetime
import json
import unittest
from unittest.mock import patch

from stock_jp.automation import nightly as n
from stock_jp.automation.paper_target import resolve
from stock_jp.automation.paper_checkpoint import run as checkpoint_run
from stock_jp.market_data.jquants import ingest
from stock_jp.operation.paper_preparation import prepare
from tests import test_provider_budget as support


class LogicalTargetTests(unittest.TestCase):
    def setUp(self):
        support.ProviderBudgetTests.setUp(self)
        config_path = self.root / 'config/sources.json'
        config = json.loads(config_path.read_text())
        config['jquants']['plan'] = 'standard'
        ingest.atomic_json(config_path, config)
        self.seed(10)

    seed = support.ProviderBudgetTests.seed
    budget = support.ProviderBudgetTests.budget
    # Reuse the synthetic Git transport, never an external provider.
    def adopted(self, *, status='COLLECTED'):
        doc = self.repo.journal()
        state = doc['runs'][self.day]
        state['status'] = status
        state['data_date'] = self.day if status == 'COLLECTED' else None
        doc['scheduled_targets'] = {str(self.context['run_id']): {'date': self.day, 'resolved_at': self.at.isoformat()}}
        record = {k: state[k] for k in ('run_date', 'status', 'data_date', 'requests', 'tasks', 'budget', 'config_sha256')}
        name = n.RECORDS + self.day + '.json'
        raw = ingest.encoded(record)
        ingest.atomic_bytes(self.root / name, raw)
        main = self.repo.tree(self.repo.refs['main'])
        main[name] = self.repo.blob(raw)
        self.repo.refs['main'] = self.repo.commit(main)
        self.context['sha'] = self.repo.refs['main']
        self.repo.workflow_sha = self.context['sha']
        tree = self.repo.tree(self.repo.refs[n.CHECKPOINT])
        tree[n.JOURNAL] = self.repo.blob(ingest.encoded(doc))
        self.repo.refs[n.CHECKPOINT] = self.repo.commit(tree)

    def cp(self):
        return n.Checkpoint(self.root, n.Git(self.repo), self.repo.refs['main'], n.scheduled_day(self.at), self.context, preview=False)

    def test_night_morning_month_year_boundaries_preserve_target_and_real_time(self):
        cases = [('2026-10-07T21:30:00+09:00', '2026-10-07'),
                 ('2026-10-08T04:17:00+09:00', '2026-10-07'),
                 ('2026-11-01T04:17:00+09:00', '2026-10-31'),
                 ('2027-01-01T04:17:00+09:00', '2026-12-31')]
        for observed, day in cases:
            with self.subTest(observed=observed):
                self.at = datetime.fromisoformat(day + 'T21:00:00+09:00')
                self.day = day
                self.seed(11)
                self.adopted()
                self.at = datetime.fromisoformat(observed)
                target = resolve(self.root, self.cp(), self.at)
                self.assertEqual(target['day'], day)
                self.assertEqual(target['cutoff_at_jst'], observed)
                started = {'status': 'in_progress', 'run_id': day, 'run_token': 'test'}
                with patch('stock_jp.operation.paper_preparation.check_readiness', return_value={'paper_go': True}), \
                     patch('stock_jp.operation.paper_preparation.start_nightly_run', return_value=started) as start, \
                     patch('stock_jp.operation.paper_preparation.fail_run', return_value={'status': 'failed'}):
                    prepare(self.root, at=self.at, logical_target=target, budget_remaining=1)
                self.assertEqual(start.call_args.kwargs['target_run_id'], day)
                self.assertEqual(start.call_args.kwargs['at'], observed)
                self.assertEqual(start.call_args.kwargs['cutoff'], observed)

    def test_restart_reuses_cutoff_even_after_next_publication_boundary(self):
        self.adopted()
        target = resolve(self.root, self.cp(), self.at)
        self.at = datetime.fromisoformat('2026-10-08T21:30:00+09:00')
        self.assertEqual(resolve(self.root, self.cp(), self.at), target)
        self.assertEqual(self.repo.journal()['scheduled_targets']['123']['date'], self.day)

    def test_second_actions_run_reuses_cutoff_before_any_inbox_save(self):
        self.adopted()
        target = resolve(self.root, self.cp(), self.at)
        self.context['run_id'] = 124
        self.at = datetime.fromisoformat('2026-10-08T04:17:00+09:00')
        self.assertEqual(resolve(self.root, self.cp(), self.at), target)

    def test_missing_adopted_record_stops(self):
        self.adopted()
        (self.root / (n.RECORDS + self.day + '.json')).unlink()
        with self.assertRaisesRegex(n.RemoteStop, 'not been adopted'):
            resolve(self.root, self.cp(), self.at)

    def test_missed_entire_night_cannot_invent_first_cutoff(self):
        self.adopted()
        self.at = datetime.fromisoformat('2026-10-08T21:30:00+09:00')
        with self.assertRaisesRegex(n.RemoteStop, 'cannot invent'):
            resolve(self.root, self.cp(), self.at)

    def test_unadopted_or_changed_record_stops_before_cutoff_is_saved(self):
        self.adopted()
        path = self.root / (n.RECORDS + self.day + '.json')
        path.write_text('{}')
        with self.assertRaisesRegex(n.RemoteStop, 'differs from main'):
            resolve(self.root, self.cp(), self.at)
        self.assertNotIn('paper_cutoff_at_jst', self.repo.journal()['scheduled_targets']['123'])

    def test_unfinished_daily_target_stops(self):
        with self.assertRaisesRegex(n.RemoteStop, 'completed daily acquisition'):
            resolve(self.root, self.cp(), self.at)

    def test_future_target_rejected(self):
        self.adopted()
        self.at = datetime.fromisoformat('2026-10-07T20:59:59+09:00')
        with self.assertRaisesRegex(n.RemoteStop, 'ahead'):
            resolve(self.root, self.cp(), self.at)

    def test_holiday_does_not_create_a_run(self):
        self.adopted(status='HOLIDAY')
        target = resolve(self.root, self.cp(), self.at)
        with patch('stock_jp.operation.paper_preparation.start_nightly_run') as start:
            self.assertEqual(prepare(self.root, at=self.at, logical_target=target)['status'], 'HOLIDAY')
            start.assert_not_called()

    def test_exhausted_budget_does_not_start_scan_but_preserves_existing_review(self):
        self.seed(11)
        self.adopted()
        budget = self.budget()
        budget.reserve('https://example.com/synthetic')
        target = resolve(self.root, budget.checkpoint, self.at)
        with patch('stock_jp.operation.paper_preparation.start_nightly_run') as start:
            result = prepare(self.root, at=self.at, logical_target=target, budget_remaining=0)
            self.assertEqual(result['status'], 'BUDGET_EXHAUSTED')
            path = self.root / 'operations/runs' / self.day / 'handoff.json'
            ingest.atomic_json(path, {'run_id': self.day, 'operation_mode': 'PAPER', 'active_rule_version': None, 'status': 'failed'})
            before = path.read_bytes()
            self.at = datetime.fromisoformat('2026-10-08T04:17:00+09:00')
            result = prepare(self.root, at=self.at, logical_target=target, budget_remaining=0)
            self.assertEqual(result['status'], 'REVIEW_REQUIRED')
            self.assertEqual(result['run']['run_id'], self.day)
            self.assertEqual(path.read_bytes(), before)
            start.assert_not_called()
        self.assertEqual(len(self.repo.journal()['paper_requests'][self.day]), 1)

    def test_cutoff_conflict_or_future_does_not_scan(self):
        self.adopted()
        target = resolve(self.root, self.cp(), self.at)
        with patch('stock_jp.operation.paper_preparation.start_nightly_run') as start:
            with self.assertRaisesRegex(ValueError, 'differs'):
                prepare(self.root, at=self.at, logical_target=target, cutoff='2026-10-07T21:01:00+09:00')
            target['cutoff_at_jst'] = '2026-10-08T04:17:00+09:00'
            with self.assertRaisesRegex(ValueError, 'invalid logical'):
                prepare(self.root, at=self.at, logical_target=target)
            start.assert_not_called()

    def test_inbox_uses_logical_day_after_midnight_and_reuses_query_cutoff(self):
        self.adopted()
        self.at = datetime.fromisoformat('2026-10-08T04:17:00+09:00')
        target = resolve(self.root, self.cp(), self.at)
        cutoff = target['cutoff_at_jst']
        from stock_jp.market_data.jquants.cache import NightlyClient
        import shutil
        cache = NightlyClient(private=self.root / 'operations', cutoff=cutoff, retrieved=cutoff,
                              plan='standard', transport=lambda *_: {'data': []})
        cache.get('/equities/master', {'date': self.day})
        ingest.atomic_json(self.root / 'operations/state.json', {'unchanged': True})
        ingest.atomic_bytes(self.root / 'operations/run-history.csv', b'status\nfailed\n')
        ingest.atomic_json(self.root / 'operations/runs' / self.day / 'source-scan-cutoff.json', {'cutoff_at_jst': cutoff})
        with patch('stock_jp.automation.paper_checkpoint.datetime') as clock:
            clock.now.return_value = self.at
            result = checkpoint_run(self.root, self.context, n.Git(self.repo), save=True, logical_target=target)
            tree = self.repo.tree(result['commit'])
            snapshot = json.loads(n.Git(self.repo).read(tree[n.PAPER_JOURNAL]))
            self.assertEqual(snapshot['day'], '2026-10-07')
            self.assertEqual(snapshot['captured_at_jst'], '2026-10-08T04:17:00+09:00')
            self.assertIn('operations/runs/2026-10-07/source-scan-cutoff.json', snapshot['files'])
            shutil.rmtree(cache.root)
            restored = checkpoint_run(self.root, self.context, n.Git(self.repo), save=False, logical_target=target)
            self.assertEqual(restored['resume_cutoff_at_jst'], cutoff)
            self.assertEqual(restored['file_count'], 2)
            replay = NightlyClient(private=self.root / 'operations', cutoff=cutoff, retrieved=self.at.isoformat(),
                                   plan='standard', transport=lambda *_: self.fail('provider call'))
            replay.get('/equities/master', {'date': self.day})
            self.assertEqual((replay.requests, replay.cache_hits), (0, 1))
        # A different Actions retry for the same day must use the saved inbox cutoff.
        self.context['run_id'] = 124
        self.at = datetime.fromisoformat('2026-10-08T05:17:00+09:00')
        (self.root / 'operations/runs' / self.day / 'source-scan-cutoff.json').unlink()
        self.assertEqual(resolve(self.root, self.cp(), self.at), target)

    def test_stopped_writer_cannot_pin_cutoff(self):
        self.adopted()
        self.repo.workflow_sha = 'f' * 40
        before = self.repo.refs[n.CHECKPOINT]
        with self.assertRaisesRegex(n.RemoteStop, 'no longer active'):
            resolve(self.root, self.cp(), self.at)
        self.assertEqual(self.repo.refs[n.CHECKPOINT], before)


class RealRunTargetTest(unittest.TestCase):
    def test_new_previous_day_run_keeps_real_lease_scan_and_handoff_time(self):
        from tests.test_nightly_operation import NightlyOperationTest, FIXTURES
        from stock_jp.operation.nightly import start_nightly_run
        fixture = NightlyOperationTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        from tests.operation_test_support import write_price_archive
        write_price_archive(fixture.root, ['9999'])
        at = '2026-09-01T04:17:00+09:00'
        result = start_nightly_run(root=fixture.root, at=at, cutoff=at,
                                   target_run_id='2026-08-31', fixture_dir=FIXTURES)
        self.assertEqual(result['run_id'], '2026-08-31')
        run = fixture.root / 'operations/runs/2026-08-31'
        handoff = json.loads((run / 'handoff.json').read_text())
        self.assertEqual(handoff['started_at_jst'], at)
        self.assertEqual(json.loads((run / 'source-scan-cutoff.json').read_text())['cutoff_at_jst'], at)
        self.assertFalse((fixture.root / 'operations/runs/2026-09-01').exists())
