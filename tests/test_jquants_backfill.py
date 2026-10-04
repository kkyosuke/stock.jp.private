from datetime import timedelta
import json
from pathlib import Path
import unittest

from stock_jp.market_data.jquants import backfill, ingest
from stock_jp.workspace import ACQUISITION_LEDGER, MARKET_WORK, project_root
from tests.operation_test_support import prepare_write
from tests import test_jquants_history as history_fixtures


class BackfillTest(unittest.TestCase):
    setUp = history_fixtures.HistoryTest.setUp
    transport = history_fixtures.HistoryTest.transport

    def ledger(self):
        return json.loads((self.root / ACQUISITION_LEDGER).read_text())

    def prepare(self):
        days = []
        current = self.end
        while len(days) < 8:
            if current.weekday() < 5:
                days.insert(0, str(current))
            current -= timedelta(days=1)
        ledger = json.loads((project_root() / ACQUISITION_LEDGER).read_text())
        ledger.update(prior_charges={}, prior_batches=0, grants=[])
        legacy = ledger['legacy_debit']
        legacy['campaign'].update(start_date=days[0], sessions=8)
        for index, entry in enumerate(legacy['batches'][0]):
            entry['target_date'] = days[index // 2]
        prepare_write(self.root / ACQUISITION_LEDGER).write_bytes(ingest.encoded(ledger))
        return ledger

    def fill(self, **kwargs):
        return backfill.run(self.root, as_of=self.end, sessions=8,
                            transport=kwargs.pop('transport', self.transport), sleep=lambda _: None, **kwargs)

    def test_fill_preserves_debits_and_complete_rerun_sends_nothing(self):
        previous = self.prepare()
        result = self.fill()
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(result['requests_used'], 18)
        self.assertEqual(self.ledger()['legacy_debit'], previous['legacy_debit'])
        self.assertEqual(sum(self.ledger()['prior_charges'].values()), 18)
        self.assertEqual(self.ledger()['prior_batches'], 2)
        calls = len(self.calls)
        self.assertEqual(self.fill(), result)
        self.assertEqual(len(self.calls), calls)

    def test_failure_is_charged_before_send_and_survives_resume(self):
        self.prepare()
        def failed(endpoint, query):
            self.assertEqual(sum(self.ledger()['prior_charges'].values()), 1)
            raise ingest.ApiError(503)
        self.assertEqual(self.fill(transport=failed)['status'], 'PAUSED')
        self.assertEqual(self.fill()['requests_used'], 19)
        self.assertEqual(sum(self.ledger()['prior_charges'].values()), 19)

    def test_crash_after_reservation_does_not_refund(self):
        self.prepare()
        def crash(*_):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.fill(transport=crash)
        self.assertEqual(sum(self.ledger()['prior_charges'].values()), 1)
        self.assertEqual(self.fill()['requests_used'], 19)

    def test_changed_shared_budget_stops_before_provider(self):
        self.prepare()
        def denied(*_):
            raise ingest.ApiError(503)
        self.fill(transport=denied)
        changed = self.ledger()
        changed['prior_batches'] += 1
        (self.root / ACQUISITION_LEDGER).write_bytes(ingest.encoded(changed))
        before = len(self.calls)
        with self.assertRaisesRegex(ingest.IngestionError, 'shared budget changed'):
            self.fill()
        self.assertEqual(len(self.calls), before)

    def test_crash_on_batch_boundary_resumes_without_refund(self):
        self.prepare()
        attempts = 0
        def interrupted(endpoint, query):
            nonlocal attempts
            attempts += 1
            if attempts == 12:
                raise KeyboardInterrupt()
            return self.transport(endpoint, query)
        with self.assertRaises(KeyboardInterrupt):
            self.fill(transport=interrupted)
        self.assertEqual(sum(self.ledger()['prior_charges'].values()), 12)
        self.assertEqual(self.fill()['requests_used'], 19)
        self.assertEqual(sum(self.ledger()['prior_charges'].values()), 19)

    def test_paginated_request_targets_and_resume(self):
        self.prepare()
        def paginated(endpoint, query):
            payload = self.transport(endpoint, query)
            if endpoint == '/equities/bars/daily':
                return payload if query.get('pagination_key') else {'data': [], 'pagination_key': 'page-two'}
            return payload
        result = self.fill(transport=paginated)
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(result['requests_used'], 26)
        self.assertEqual(sum(self.ledger()['prior_charges'].values()), 26)
        state = json.loads((self.root / MARKET_WORK / 'jquants/history/state.json').read_text())
        self.assertEqual(len(state['tasks']), 16)
