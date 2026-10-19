"""Malformed offline accounting is rejected, never repaired or normalized."""
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stock_jp.market_data.jquants import ingest
from stock_jp.operation.paper_offline_budget import validate_budget

DAY = '2026-10-06'
CUTOFF = '2026-10-07T02:30:01+09:00'
START = '2026-10-07T02:00:00+09:00'


def checkpoint(root):
    state = {'daily_runner_version': 1, 'run_date': DAY, 'started_at': START,
             'config_sha256': ingest.digest((root / 'config/sources.json').read_bytes()), 'budget': 12,
             'requests': [{'endpoint': '/markets/calendar', 'query_sha256': 'a' * 64, 'reserved_at': START}],
             'tasks': {f'trading_calendar:{DAY}': {'manifest_sha256': 'b' * 64, 'rows': 1, 'timing': 'observed'}},
             'status': 'HOLIDAY', 'data_date': None, 'error': None, 'http_status': None, 'retry_not_before': None}
    empty = ingest.digest(ingest.encoded([]))
    return {'version': 1, 'files': {}, 'github_budget': None, 'runs': {DAY: state},
            'scheduled_targets': {'123': {'date': DAY, 'resolved_at': START, 'paper_cutoff_at_jst': CUTOFF}},
            'provider_debit_bases': {DAY: {'prior_requests': 0, 'legacy_requests': 0, 'legacy_receipts_sha256': empty,
                                         'initial_daily_count': 0, 'initial_daily_sha256': empty}},
            'paper_requests': {DAY: [{'sequence': i, 'host': 'allowed', 'reserved_at': CUTOFF,
                                     'action_run_id': 123, 'execution_sha': 'a' * 40} for i in range(1, 12)]}}


class OfflineBudgetTest(unittest.TestCase):
    def test_valid_twelve_reservations_are_preserved(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.config(root)
            value = checkpoint(root)
            before = copy.deepcopy(value)
            validate_budget(root, value, day=DAY, cutoff=CUTOFF)
            self.assertEqual(value, before)

    def config(self, root):
        ingest.atomic_json(root / 'config/sources.json', {'jquants': {'ingestion': {'max_requests_per_run': 12}}})
        ingest.atomic_json(root / 'operations/acquisition/date-grants.json', {'prior_charges': {}})

    def test_unknown_incomplete_or_inconsistent_evidence_is_rejected_without_mutation(self):
        cases = ('missing_bases', 'missing_paper', 'missing_basis_day', 'missing_paper_day', 'daily13_without_mappings',
                 'daily13', 'total13', 'wrong_day', 'bad_budget_type', 'bad_config', 'missing_request_field',
                 'bad_endpoint', 'bad_query', 'bad_daily_time', 'daily_after_cutoff', 'wrong_initial_hash',
                 'bad_initial_count', 'bad_legacy_hash', 'bad_prior', 'bad_paper_sequence', 'bad_paper_host',
                 'bad_paper_sha', 'bad_paper_owner', 'missing_owner', 'wrong_owner_day', 'paper_before_cutoff',
                 'future_paper', 'missing_binding_time', 'unknown_binding_field', 'missing_ledger',
                 'missing_calendar', 'inconsistent_data_date', 'unknown_initial_overlap')
        for case in cases:
            with self.subTest(case=case), TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.config(root)
                value = checkpoint(root)
                state, basis = value['runs'][DAY], value['provider_debit_bases'][DAY]
                receipt, paper = state['requests'][0], value['paper_requests'][DAY][0]
                if case.startswith('missing_bases'): del value['provider_debit_bases']
                elif case == 'missing_paper': del value['paper_requests']
                elif case == 'missing_basis_day': del value['provider_debit_bases'][DAY]
                elif case == 'missing_paper_day': del value['paper_requests'][DAY]
                elif case.startswith('daily13'):
                    state['requests'] *= 13
                    if case.endswith('without_mappings'):
                        del value['provider_debit_bases']; del value['paper_requests']
                elif case == 'total13': value['paper_requests'][DAY].append(dict(paper, sequence=12))
                elif case == 'wrong_day': state['run_date'] = '2026-10-05'
                elif case == 'bad_budget_type': state['budget'] = True
                elif case == 'bad_config': state['config_sha256'] = 'c' * 64
                elif case == 'missing_request_field': del receipt['query_sha256']
                elif case == 'bad_endpoint': receipt['endpoint'] = '/unknown'
                elif case == 'bad_query': receipt['query_sha256'] = 'unknown'
                elif case == 'bad_daily_time': receipt['reserved_at'] = '2026-10-07T02:00:00'
                elif case == 'daily_after_cutoff': receipt['reserved_at'] = '2026-10-07T03:00:00+09:00'
                elif case == 'wrong_initial_hash': basis['initial_daily_sha256'] = 'c' * 64
                elif case == 'bad_initial_count': basis['initial_daily_count'] = True
                elif case == 'bad_legacy_hash': basis['legacy_receipts_sha256'] = 'c' * 64
                elif case == 'bad_prior': basis['prior_requests'] = -1
                elif case == 'bad_paper_sequence': paper['sequence'] = 2
                elif case == 'bad_paper_host': paper['host'] = 'https://untrusted'
                elif case == 'bad_paper_sha': paper['execution_sha'] = 'unknown'
                elif case == 'bad_paper_owner': paper['action_run_id'] = True
                elif case == 'missing_owner': paper['action_run_id'] = 456
                elif case == 'wrong_owner_day': value['scheduled_targets']['123']['date'] = '2026-10-05'
                elif case == 'paper_before_cutoff': paper['reserved_at'] = START
                elif case == 'future_paper': paper['reserved_at'] = '9999-01-01T00:00:00+09:00'
                elif case == 'missing_binding_time': del value['scheduled_targets']['123']['resolved_at']
                elif case == 'unknown_binding_field': value['scheduled_targets']['123']['unknown'] = True
                elif case == 'missing_calendar': state['tasks'] = {}
                elif case == 'inconsistent_data_date': state['data_date'] = DAY
                elif case == 'unknown_initial_overlap':
                    basis['initial_daily_count'] = 1
                    basis['initial_daily_sha256'] = ingest.digest(ingest.encoded(state['requests']))
                    basis['prior_requests'] = 1
                    value['paper_requests'][DAY].pop()
                    ingest.atomic_json(root / 'operations/acquisition/date-grants.json', {'prior_charges': {DAY: 1}})
                elif case == 'missing_ledger': (root / 'operations/acquisition/date-grants.json').unlink()
                before = copy.deepcopy(value)
                files = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
                with self.assertRaisesRegex(ValueError, 'offline budget evidence rejected'):
                    validate_budget(root, value, day=DAY, cutoff=CUTOFF)
                self.assertEqual(value, before)
                self.assertEqual(files, {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()})
