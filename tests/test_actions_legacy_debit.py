"""Known old requests consume capacity; rejected pages never enter a checkpoint."""
from tests.operation_test_support import prepare_write
from stock_jp.workspace import record_path
import copy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch
import stock_jp.automation.legacy_debit as debit
import stock_jp.automation.targets as targets
import stock_jp.automation.daily_remote as remote
import stock_jp.market_data.jquants.ingest as ingest
import tests.test_jquants_history as fixture


class LegacyDebitTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.HistoryTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.run_history()
        self.source = record_path(self.fixture.private.parent, 'market-data')
        self.value = debit.prepare(self.source, 'a'*40)

    def test_read_only_preparation_carries_all_reservations_without_payload(self):
        before = {str(p):p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        value = debit.prepare(self.source, 'a'*40)
        self.assertEqual(before, {str(p):p.read_bytes() for p in self.source.rglob('*') if p.is_file()})
        self.assertEqual(debit.charges(value), 12)
        self.assertEqual(debit.charges(value, value['campaign']['start_date']), 4)
        self.assertEqual(value['payload_disposition'], 'REJECTED_NOT_ADOPTED')
        self.assertNotIn('pages', json.dumps(value))
        self.assertNotIn('tasks', value)

    def test_unknown_response_blocks_preparation(self):
        path = self.source/'jquants/history/state.json'
        state = json.loads(path.read_bytes())
        state['batches'][0][-1]['query_sha256'] = 'f'*64
        prepare_write(path).write_text(json.dumps(state))
        with self.assertRaises(remote.RemoteStop): debit.prepare(self.source, 'a'*40)

    def test_corrupt_response_blocks_preparation(self):
        path = next((self.source/'jquants/objects').rglob('*.json'))
        prepare_write(path).write_text('{}')
        with self.assertRaises((ValueError, remote.RemoteStop, ingest.IngestionError)):
            debit.prepare(self.source, 'a'*40)

    def test_partial_checkpoint_blocks_preparation(self):
        path = self.source/'jquants/datasets/equities_master/2026-09-23/checkpoint.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        prepare_write(path).write_text('{}')
        with self.assertRaises(remote.RemoteStop): debit.prepare(self.source, 'a'*40)

    def checkpoint(self, value, *, used=0):
        checkpoint = object.__new__(targets.TargetCheckpoint)
        checkpoint.day = value['campaign']['start_date']
        checkpoint.profile = 'history'
        checkpoint.document = {'targets': {'legacy_debit':value, 'batches':[[]]},
            'runs': {checkpoint.day: {'reservation_ids':['x']*used}}}
        return checkpoint

    def test_twelve_per_date_includes_legacy_and_reserve_rechecks(self):
        checkpoint = self.checkpoint(self.value, used=8)
        with patch.object(remote.RunnerCheckpoint, 'before_reservation'):
            with self.assertRaises(ingest.RequestBudgetExhausted): checkpoint.before_reservation()
        checkpoint.document['runs'][checkpoint.day]['reservation_ids'].pop()
        with patch.object(remote.RunnerCheckpoint, 'before_reservation'):
            checkpoint.before_reservation()

    def test_campaign_six_hundred_and_fifty_batches_include_legacy(self):
        value = copy.deepcopy(self.value)
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        receipt = value['batches'][0][0]
        entries = [dict(target_date=receipt['target_date'], receipt=dict(receipt['receipt'],
            reserved_at=(now+timedelta(seconds=i)).isoformat())) for i in range(600)]
        value['batches'] = [entries[i:i+12] for i in range(0,600,12)]
        debit.validate(value, value['campaign'])
        checkpoint = self.checkpoint(value)
        checkpoint.day = value['campaign']['as_of']
        checkpoint.document['runs'][checkpoint.day] = {'reservation_ids':[]}
        with patch.object(remote.RunnerCheckpoint, 'before_reservation'):
            with self.assertRaises(ingest.RequestBudgetExhausted): checkpoint.before_reservation()
        # A partly consumed old batch is retained, never recycled as a new batch.
        value['batches'][-1].pop()
        with patch.object(remote.RunnerCheckpoint, 'before_reservation'):
            with self.assertRaises(ingest.RequestBudgetExhausted): checkpoint.before_reservation()

    def test_duplicate_legacy_receipt_rejected(self):
        value = copy.deepcopy(self.value)
        value['batches'][0][1] = value['batches'][0][0]
        with self.assertRaises(remote.RemoteStop): debit.validate(value, value['campaign'])


if __name__ == '__main__': unittest.main()
