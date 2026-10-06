import copy
import shutil
import unittest

from stock_jp.automation import nightly as n
from stock_jp.automation.provider_budget import PaperBudget, shared_usage
from stock_jp.market_data.jquants import ingest
import tests.test_nightly_market_data as fixture


class CumulativeProviderBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.NightlyMarketDataTest(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.repo = self.fixture.root, self.fixture.repo
        self.day = self.fixture.fixture.at.date().isoformat()
        self.ledger = self.root / 'operations/acquisition/date-grants.json'

    def paper(self):
        return PaperBudget(self.root, self.fixture.context, n.Git(self.repo), preview=True,
                           clock=lambda: self.fixture.fixture.at)

    def debit(self, count):
        ingest.atomic_json(self.ledger, {'prior_charges': {self.day: count}})

    def rewrite_journal(self, document):
        tree = self.repo.tree(self.repo.refs[n.CHECKPOINT])
        tree[n.JOURNAL] = self.repo.blob(ingest.encoded(document))
        self.repo.refs[n.CHECKPOINT] = self.repo.commit(tree)

    def test_partial_legacy_two_plus_nine_daily_leaves_only_one_after_restart(self):
        self.debit(2)
        first = self.fixture.run_preview()
        self.assertEqual((first['requests_used'], first['requests_for_date']), (9, 11))
        pull_request = next(body for method, path, body in self.repo.calls if method == 'POST' and path.endswith('/pulls'))
        self.assertIn('通信: 11/12', pull_request['body'])
        self.paper().reserve('https://example.com/offline')
        with self.assertRaises(ingest.RequestBudgetExhausted):
            self.paper().reserve('https://example.com/offline')
        # Existing local nine-request archive must not replace the fixed debit.
        again = self.fixture.run_preview()
        self.assertEqual((again['requests_used'], again['requests_for_date'], again['paper_requests_used']), (9, 12, 1))
        self.assertEqual(len(self.fixture.fixture.calls), 9)
        self.assertEqual(shared_usage(self.repo.journal(), self.day), 12)

    def test_failed_daily_reservation_and_clean_runner_retain_legacy_debit(self):
        self.debit(2)
        def crash(*_):
            self.assertEqual(shared_usage(self.repo.journal(), self.day), 3)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.fixture.run_preview(crash)
        shutil.rmtree(self.fixture.fixture.archive.parent)
        result = self.fixture.run_preview()
        self.assertEqual((result['requests_used'], result['requests_for_date']), (10, 12))
        with self.assertRaises(ingest.RequestBudgetExhausted):
            self.paper().reserve('https://example.com/offline')

    def test_existing_daily_and_paper_are_counted_once_without_external_debit(self):
        self.fixture.run_preview()
        self.paper().reserve('https://example.com/offline')
        old = self.repo.journal()
        del old['provider_debit_bases']  # Pre-fix journal with no external charge.
        self.rewrite_journal(old)
        self.paper().reserve('https://example.com/offline')
        again = self.fixture.run_preview()
        self.assertEqual((again['requests_used'], again['requests_for_date']), (9, 11))
        self.assertEqual(len(self.fixture.fixture.calls), 9)

    def test_unanchored_partial_debit_overlap_requires_reconciliation(self):
        self.debit(2)
        self.fixture.run_preview()
        old = self.repo.journal()
        del old['provider_debit_bases']
        self.rewrite_journal(old)
        saved = copy.deepcopy(self.repo.journal()['runs'])
        for operation in (self.paper, self.fixture.run_preview):
            with self.assertRaisesRegex(n.RemoteStop, 'overlap is unknown'):
                operation()
        self.assertEqual(self.repo.journal()['runs'], saved)
        self.assertEqual(len(self.fixture.fixture.calls), 9)

    def test_rejected_legacy_payload_receipt_is_also_a_separate_debit(self):
        legacy = {'version': 1, 'source_commit': 'e' * 40, 'journal_sha256': 'd' * 64,
            'campaign': {'start_date': self.day, 'as_of': self.day, 'sessions': 1},
            'payload_disposition': 'REJECTED_NOT_ADOPTED',
            'batches': [[{'target_date': self.day, 'receipt': {'endpoint': '/markets/calendar',
                'query_sha256': 'c' * 64, 'reserved_at': self.fixture.fixture.at.isoformat()}}]]}
        ingest.atomic_json(self.ledger, {'prior_charges': {self.day: 1}, 'legacy_debit': legacy})
        result = self.fixture.run_preview()
        self.assertEqual(result['requests_for_date'], 11)
        self.paper().reserve('https://example.com/offline')
        with self.assertRaises(ingest.RequestBudgetExhausted):
            self.paper().reserve('https://example.com/offline')
        basis = self.repo.journal()['provider_debit_bases'][self.day]
        self.assertEqual((basis['prior_requests'], basis['legacy_requests']), (1, 1))

    def test_changing_or_refunding_historical_debit_cannot_reissue_allowance(self):
        self.debit(2)
        self.fixture.run_preview()
        for replacement in (0, 3):
            self.debit(replacement)
            for operation in (self.paper, self.fixture.run_preview):
                with self.assertRaisesRegex(n.RemoteStop, 'debit changed'):
                    operation()
        self.assertEqual(len(self.fixture.fixture.calls), 9)
