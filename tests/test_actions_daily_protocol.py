"""Synthetic crash/restart tests: no credential, real API, or remote writes."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('actions_daily_protocol', ROOT / 'scripts/actions_daily_protocol.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
DAY = '2026-10-03'
AT = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
MAIN = 'a' * 40
CONFIG = 'b' * 64
PAGE = 'c' * 64


class MemoryRemote:
    def __init__(self):
        self.revision = 0
        self.state = {'runs': {}}
        self.lose_ack = False
        self.reject = False

    def read(self):
        return str(self.revision), copy.deepcopy(self.state)

    def cas(self, expected, value):
        if self.reject or expected != str(self.revision):
            raise p.Stopped('stale parent; no force update')
        self.state = copy.deepcopy(value)
        self.revision += 1
        if self.lose_ack:
            raise p.Stopped('write acknowledgement lost')
        return str(self.revision)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.remote = MemoryRemote()
        self.boundary = p.DurableBoundary(self.remote)
        self.boundary.begin(DAY, MAIN, CONFIG)
        self.calls = 0

    def test_history_design_distinguishes_adopted_terminal_without_enabling_adapter(self):
        journal = {'status': 'COMPLETE', 'campaign': 'synthetic-only'}
        evidence = dict(repository=p.REPOSITORY, visibility='private', publication_rejected=False,
                        unresolved_campaign=False, publication_authorized=True,
                        main_sha=MAIN, approved_main_sha=MAIN, generation=PAGE, approved_generation=PAGE,
                        journal_sha256=p.checksum(journal), terminal_semantics_verified=True,
                        marker_matches_journal=True, all_file_hashes_verified=True,
                        main_contains_generation=True, approval_independently_verified=True)
        result = p.history_transition_plan(journal, evidence)
        self.assertEqual(result['classification'], 'ADOPTED_TERMINAL_DESIGN_BASELINE')
        self.assertFalse(result['execution_allowed'])
        self.assertTrue(result['retain_history_evidence'])
        for status in ('RUNNING', 'PAUSED', 'BATCH_EXHAUSTED', 'BLOCKED'):
            with self.assertRaises(p.Stopped):
                p.history_transition_plan({**journal, 'status': status}, evidence)
        for key, value in [('publication_rejected', True), ('unresolved_campaign', True),
                           ('approved_main_sha', 'd' * 40), ('approved_generation', 'd' * 64),
                           ('journal_sha256', 'd' * 64), ('terminal_semantics_verified', False),
                           ('marker_matches_journal', False), ('all_file_hashes_verified', False),
                           ('main_contains_generation', False), ('approval_independently_verified', False)]:
            with self.assertRaises(p.Stopped):
                p.history_transition_plan(journal, {**evidence, key: value})
        with self.assertRaisesRegex(p.Stopped, 'journal binding'):
            p.history_transition_plan({**journal, 'campaign': 'forged'}, evidence)

    def send(self, endpoint, query):
        self.assertEqual(self.remote.state['runs'][DAY]['status'], 'IN_FLIGHT')
        self.calls += 1
        return {'synthetic': True}

    def request(self, at=AT, checkpoint=lambda response: PAGE):
        return self.boundary.request(DAY, '/equities/bars/daily', {'date': DAY}, at, self.send, checkpoint)

    def test_budget_survives_new_instances_and_same_day_reruns(self):
        for _ in range(12):
            self.boundary = p.DurableBoundary(self.remote)
            self.assertEqual(self.boundary.begin(DAY, MAIN, CONFIG), 'READY')
            self.request()
        with self.assertRaisesRegex(p.Stopped, 'budget exhausted'):
            self.request()
        self.assertEqual(self.calls, 12)
        self.assertEqual(len(self.remote.state['runs'][DAY]['requests']), 12)

    def test_lost_reservation_ack_never_sends_or_refunds(self):
        self.remote.lose_ack = True
        with self.assertRaises(p.Stopped):
            self.request()
        self.assertEqual(self.calls, 0)
        self.remote.lose_ack = False
        self.boundary = p.DurableBoundary(self.remote)
        with self.assertRaisesRegex(p.Stopped, 'ambiguous'):
            self.request()
        self.assertEqual(len(self.remote.state['runs'][DAY]['requests']), 1)

    def test_stale_writer_never_sends(self):
        self.remote.reject = True
        with self.assertRaises(p.Stopped):
            self.request()
        self.assertEqual(self.calls, 0)

    def test_invalid_page_ack_stops_without_repeat(self):
        with self.assertRaisesRegex(p.Stopped, 'checkpoint'):
            self.request(checkpoint=lambda response: None)
        with self.assertRaisesRegex(p.Stopped, 'ambiguous'):
            self.request()
        self.assertEqual(self.calls, 1)

    def test_crash_after_send_stays_charged(self):
        def crash(response):
            raise OSError('synthetic checkpoint outage')
        with self.assertRaises(OSError):
            self.request(checkpoint=crash)
        self.assertEqual(self.remote.state['runs'][DAY]['status'], 'IN_FLIGHT')
        with self.assertRaises(p.Stopped):
            p.DurableBoundary(self.remote).request(DAY, '/equities/master', {}, AT, self.send, lambda r: PAGE)
        self.assertEqual(self.calls, 1)

    def test_readback_mismatch_prevents_send(self):
        original = self.remote.read
        def inconsistent():
            revision, value = original()
            return ('wrong' if int(revision) > 1 else revision), value
        self.remote.read = inconsistent
        with self.assertRaisesRegex(p.Stopped, 'readback'):
            self.request()
        self.assertEqual(self.calls, 0)

    def test_429_waits_120_seconds_and_retry_uses_second_reservation(self):
        with self.assertRaises(p.Stopped):
            self.request(checkpoint=lambda r: None)
        self.boundary.reconcile_http_error(DAY, 429, AT)
        with self.assertRaisesRegex(p.Stopped, 'cooldown'):
            self.request(at=AT + timedelta(seconds=119))
        self.request(at=AT + timedelta(seconds=120))
        self.assertEqual(self.calls, 2)
        self.assertEqual(len(self.remote.state['runs'][DAY]['requests']), 2)

    def test_permission_denial_is_terminal_for_automatic_retry(self):
        with self.assertRaises(p.Stopped):
            self.request(checkpoint=lambda r: None)
        self.boundary.reconcile_http_error(DAY, 403, AT)
        with self.assertRaises(p.Stopped):
            self.request()
        self.assertEqual(self.calls, 1)

    def test_no_budget_reset_at_next_date_or_changed_inputs(self):
        with self.assertRaisesRegex(p.Stopped, 'earlier run'):
            self.boundary.begin('2026-10-04', MAIN, CONFIG)
        with self.assertRaisesRegex(p.Stopped, 'inputs changed'):
            self.boundary.begin(DAY, 'd' * 40, CONFIG)
        with self.assertRaisesRegex(p.Stopped, 'JST date changed'):
            self.request(at=AT + timedelta(hours=4))
        self.assertEqual(self.calls, 0)

    def test_terminal_same_day_is_deduplicated(self):
        self.remote.state['runs'][DAY]['status'] = 'RESTORED'
        self.assertEqual(self.boundary.begin(DAY, MAIN, CONFIG), 'RESTORED')
        with self.assertRaises(p.Stopped):
            self.request()
        with self.assertRaisesRegex(p.Stopped, 'out-of-order'):
            self.boundary.begin('2026-10-02', MAIN, CONFIG)
        self.assertEqual(self.boundary.begin('2026-10-04', MAIN, CONFIG), 'READY')

    def test_restore_requires_verified_merge_generation_and_every_hash(self):
        with self.assertRaises(p.Stopped):
            p.restored(self.remote, DAY, PAGE, PAGE, True)
        self.remote.state['runs'][DAY]['status'] = 'MERGED_PENDING_RESTORE'
        for generation, hashes in [('d' * 64, True), (PAGE, False)]:
            with self.assertRaises(p.Stopped):
                p.restored(self.remote, DAY, PAGE, generation, hashes)
        p.restored(self.remote, DAY, PAGE, PAGE, True)
        self.assertEqual(self.remote.state['runs'][DAY]['status'], 'RESTORED')

    def test_preflight_defaults_and_rejected_publication_fail_closed(self):
        self.assertFalse(p.preflight({})['eligible'])
        facts = dict.fromkeys(['enabled', 'publication_authorized', 'old_scheduler_stopped',
                              'exclusive_writer', 'main_restored_and_verified', 'checkpoint_verified',
                              'ci_credential_verified', 'live_adapter_reviewed'], True)
        facts.update(dict.fromkeys(['unresolved_history', 'publication_rejected', 'open_daily_pr',
                                   'unknown_unsaved_results'], False))
        facts.update(repository=p.REPOSITORY, visibility='private', plan='light', mode='PAPER',
                     submission='HUMAN_ONLY', price_coverage_min=.98, active_coverage_min=1.0)
        self.assertTrue(p.preflight(facts)['eligible'])
        for field in ('publication_rejected', 'unresolved_history'):
            self.assertFalse(p.preflight({**facts, field: True})['eligible'])

    def test_publication_requires_exact_successful_head_and_reviews(self):
        evidence = dict(repository=p.REPOSITORY, visibility='private', semantic_proof_valid=True,
                        publication_authorized=True, publication_rejected=False, head=MAIN,
                        verified_head=MAIN, base=MAIN, main=MAIN, all_pages_verified=True,
                        reviews_satisfied=True, mergeable=True, only_archive_paths=True,
                        checks=[dict(name=name, head=MAIN, app='github-actions', status='completed',
                                     conclusion='success') for name in
                                ('format', 'test', 'guardrail', 'ownership-guardrail')])
        self.assertEqual(p.publication_gate(evidence)['expected_head_sha'], MAIN)
        for field, value in [('verified_head', 'd' * 40), ('base', 'd' * 40),
                             ('all_pages_verified', False), ('reviews_satisfied', False),
                             ('publication_rejected', True), ('only_archive_paths', False)]:
            with self.assertRaises(p.Stopped):
                p.publication_gate({**evidence, field: value})
        evidence['checks'].append(dict(name='additional-check', head=MAIN, app='github-actions',
                                       status='completed', conclusion='failure'))
        with self.assertRaises(p.Stopped):
            p.publication_gate(evidence)


if __name__ == '__main__':
    unittest.main()
