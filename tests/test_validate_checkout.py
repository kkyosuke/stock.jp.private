from stock_jp.workspace import project_root
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = project_root()
spec = importlib.util.spec_from_file_location('validate_checkout', ROOT / 'src/stock_jp/automation/checkout.py')
checkout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkout)


class CheckoutValidationTest(unittest.TestCase):
    def responses(self, **overrides):
        policy = {'valid': True, 'operation_mode': 'PAPER', 'broker_submission': 'HUMAN_ONLY',
                  'live_orders_allowed': False, 'limited_live_orders_allowed': False}
        policy.update(overrides)
        return [{'valid': True}, policy]

    def test_static_success_never_claims_runtime_ready(self):
        with patch.object(checkout, 'command_json', side_effect=self.responses()) as command:
            result = checkout.validate_checkout()
        self.assertTrue(result['valid'])
        self.assertEqual(result['runtime_readiness'], 'NOT_EVALUATED')
        self.assertIsNone(result['paper_go'])
        self.assertEqual([call.args[0] for call in command.call_args_list], ['stock_jp.operation.state', 'stock_jp.operation.policy'])
        self.assertTrue(all(call.kwargs == {'include_market_data': False} for call in command.call_args_list))

    def test_invalid_schema_mode_submission_and_live_permission_fail(self):
        for changed in [{'valid': False}, {'operation_mode': 'LIVE'}, {'broker_submission': 'AUTOMATIC'},
                        {'live_orders_allowed': True}, {'limited_live_orders_allowed': True}]:
            with self.subTest(changed=changed), patch.object(checkout, 'command_json', side_effect=self.responses(**changed)):
                self.assertFalse(checkout.validate_checkout()['valid'])
        with patch.object(checkout, 'command_json', side_effect=[{'valid': False}, self.responses()[1]]):
            self.assertFalse(checkout.validate_checkout()['valid'])

    def test_required_workflow_distinguishes_static_synthetic_and_runtime(self):
        text = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertIn('python -m stock_jp automation validate', text)
        self.assertIn('python -m stock_jp operation smoke --days 20 --start-date 2026-09-01', text)
        self.assertNotIn('stock_jp.operation.readiness', text)
        self.assertNotIn('paper_go', text)
        self.assertNotIn('--report-only', text)


class PersistedRunMonitoringTest(unittest.TestCase):
    def setUp(self):
        from stock_jp.automation.health import persisted_run_report, parse_time
        self.report = persisted_run_report
        self.now = parse_time('2026-10-01T22:00:00+09:00')
        self.state = {'last_run_id': '2026-10-01', 'last_successful_run_at_jst': '2026-10-01T21:30:00+09:00',
                      'last_disclosure_cutoff_jst': '2026-10-01T21:00:00+09:00', 'next_run_at_jst': '2026-10-02T21:00:00+09:00'}
        self.row = {'run_id': '2026-10-01', 'attempt': '1', 'status': 'COMPLETED', 'operation_mode': 'PAPER',
                    'completed_at_jst': self.state['last_successful_run_at_jst'],
                    'source_cutoff_jst': self.state['last_disclosure_cutoff_jst']}

    def test_current_saved_run_is_observed_without_runtime_ready_claim(self):
        result = self.report(self.state, {'status': 'OK', 'alerts': []}, [self.row], self.now)
        self.assertEqual(result['alerts'], [])
        self.assertEqual(result['monitoring_scope'], 'PERSISTED_RUN_METADATA_ONLY')
        self.assertEqual(result['runtime_readiness'], 'NOT_EVALUATED')
        self.assertIsNone(result['paper_go'])

    def test_missing_stale_failed_inconsistent_future_metadata_alerts(self):
        from datetime import timedelta
        cases = [({}, [self.row], self.now), (self.state, [], self.now),
                 (self.state, [self.row], self.now + timedelta(days=5)),
                 (self.state, [{**self.row, 'status': 'FAILED'}], self.now),
                 (self.state, [{**self.row, 'run_id': '2026-10-02'}], self.now),
                 (self.state, [self.row], self.now - timedelta(days=1))]
        for state, rows, now in cases:
            with self.subTest(state=state, rows=rows, now=now):
                result = self.report(state, {'status': 'OK', 'alerts': []}, rows, now)
                self.assertTrue(result['alerts'])
                self.assertIsNone(result['paper_go'])

    def test_scheduled_workflow_reads_only_persisted_metadata(self):
        text = (ROOT / '.github/workflows/operation-health.yml').read_text()
        self.assertIn('python -m stock_jp automation health --persisted-only --output', text)
        self.assertNotIn('--report-only', text)
