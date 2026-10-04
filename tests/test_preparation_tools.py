from tests.operation_test_support import prepare_write
from stock_jp.workspace import record_path
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch
import stock_jp.operation.recovery as r


class RecoveryTest(unittest.TestCase):
    def test_missing_paper_does_not_become_formal_recovery_success(self):
        with TemporaryDirectory() as directory:
            roots=[Path(directory)/n for n in ('source','recovered')]
            for root in roots:
                (root/'.git').mkdir(parents=True)
                private=root / 'operations';private.mkdir(parents=True)
                (prepare_write(record_path(private.parent, 'state.json'))).write_text('{}')
                (prepare_write(record_path(private.parent, 'capital-ledger.csv'))).write_text('event_id\n')
                (prepare_write(record_path(private.parent, 'run-history.csv'))).write_text('run_id,status,operation_mode\n')
            def git(root,*args):
                if args==('remote','get-url','origin'):return 'https://github.com/'+r.REPO+'.git'
                if args==('branch','--show-current'):return 'main'
                if args==('status','--porcelain'):return ''
                return 'a'*40 if args[-1]=='HEAD' else 'b'*40
            def github(command,**kwargs):
                return json.dumps({'private':True} if command[-1].endswith(r.REPO) else {'object':{'sha':'a'*40}})
            with patch.object(r,'git',side_effect=git),patch.object(r.subprocess,'check_output',side_effect=github),patch.object(r,'_bootstrap',return_value={'paper_go':True}),patch.object(r,'validate_workspace',return_value=[]),patch.object(r,'evaluate_official_coverage',return_value={'eligible':False}),patch('stock_jp.operation.provenance.capture',side_effect=ValueError('synthetic uncommitted clone')) as capture:
                result=r.generate(*roots,'synthetic-test-operator')
            self.assertEqual(capture.call_args.kwargs['inspection_at_jst'],result['started_at_jst'])
            self.assertEqual(result['repository_layout_revision'],5)
            self.assertEqual(result['source_code_tree'],'b'*40)
            self.assertFalse(result['checks']['latest_successful_run'])
            self.assertFalse(result['checks']['latest_handoff'])
    def test_same_checkout_is_rejected(self):
        with self.assertRaises(ValueError):r.generate(Path('/tmp'),Path('/tmp'),'test')

class CanonicalRecoveryRunTest(unittest.TestCase):
    def setUp(self):
        import csv
        from tests.test_live_gate_evidence import OfficialCoverageEvidenceTest
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        OfficialCoverageEvidenceTest()._write_coverage(self.root)
        self.path=self.root / 'operations/run-history.csv'
        with self.path.open() as stream:self.rows=list(csv.DictReader(stream))
        for row in self.rows:
            handoff={'run_id':row['run_id'],'attempt':int(row['attempt']),'status':'completed',
                     'operation_mode':'PAPER','active_rule_version':'v0.4','completed_at_jst':row['completed_at_jst']}
            (prepare_write(self.root / 'operations/runs' / row['run_id'] / 'handoff.json')).write_text(json.dumps(handoff))
    def write_rows(self,rows):
        import csv
        with prepare_write(self.path).open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=self.rows[0]);writer.writeheader();writer.writerows(rows)
    def test_out_of_order_and_old_rule_share_canonical_run(self):
        latest=self.rows[-1];old=dict(latest,run_id='2099-01-01',price_date='2099-01-01',active_rule_version='v0.3')
        self.write_rows([*reversed(self.rows),old])
        with patch('stock_jp.operation.evidence.validate_run_artifacts') as source_check,patch.object(r,'validate_run_artifacts') as run_check:
            coverage,selected,ok,errors=r.validated_paper_run(self.root)
        self.assertTrue(ok,errors);self.assertEqual(selected['run_id'],latest['run_id'])
        self.assertEqual(source_check.call_args.kwargs['run_id'],run_check.call_args.kwargs['run_id'])
        self.assertEqual(selected['attempt'],latest['attempt']);self.assertIn('handoff_sha256',selected)
        self.assertIn('handoff.json',selected['artifact_hashes'])
    def test_failed_retry_and_wrong_attempt_cannot_use_old_handoff(self):
        latest=self.rows[-1];self.write_rows([*self.rows,dict(latest,attempt='2',status='FAILED')])
        with patch('stock_jp.operation.evidence.validate_run_artifacts'),patch.object(r,'validate_run_artifacts'):
            _,selected,_,_=r.validated_paper_run(self.root)
        self.assertNotEqual(selected['run_id'],latest['run_id'])
        self.write_rows([*self.rows,dict(latest,attempt='2')])
        with patch('stock_jp.operation.evidence.validate_run_artifacts'),patch.object(r,'validate_run_artifacts') as integrity:
            _,selected,ok,errors=r.validated_paper_run(self.root)
        self.assertFalse(ok);self.assertIn('attempt',errors[0]);integrity.assert_not_called()
    def test_handmade_completed_handoff_is_not_run_integrity(self):
        with patch('stock_jp.operation.evidence.validate_run_artifacts'):
            _,selected,ok,errors=r.validated_paper_run(self.root)
        self.assertFalse(ok);self.assertTrue(errors)
