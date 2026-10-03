from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch
from scripts import recovery_drill as r


class RecoveryTest(unittest.TestCase):
    def test_missing_paper_does_not_become_formal_recovery_success(self):
        with TemporaryDirectory() as directory:
            roots=[Path(directory)/n for n in ('source','recovered')]
            for root in roots:
                (root/'.git').mkdir(parents=True)
                private=root/'operations/private';private.mkdir(parents=True)
                (root/'stock.jp/operations').mkdir(parents=True)
                (root/'stock.jp/operations/private').symlink_to(private,target_is_directory=True)
                (private/'state.json').write_text('{}')
                (private/'capital-ledger.csv').write_text('event_id\n')
                (private/'run-history.csv').write_text('run_id,status,operation_mode\n')
            def git(root,*args):
                if args==('remote','get-url','origin'):return 'https://github.com/'+r.REPO+'.git'
                if args==('branch','--show-current'):return 'main'
                if args==('status','--porcelain'):return ''
                return 'a'*40 if args[-1]=='HEAD' else 'b'*40
            def github(command,**kwargs):
                return json.dumps({'private':True} if command[-1].endswith(r.REPO) else {'object':{'sha':'a'*40}})
            with patch.object(r,'git',side_effect=git),patch.object(r.subprocess,'check_output',side_effect=github),patch.object(r,'_bootstrap',return_value={'paper_go':True}),patch.object(r,'validate_workspace',return_value=[]),patch.object(r,'evaluate_official_coverage',return_value={'eligible':False}):
                result=r.generate(*roots,'synthetic-test-operator')
            self.assertEqual(result['repository_layout_revision'],4)
            self.assertEqual(result['source_code_tree'],'b'*40)
            self.assertFalse(result['checks']['latest_successful_run'])
            self.assertFalse(result['checks']['latest_handoff'])
    def test_same_checkout_is_rejected(self):
        with self.assertRaises(ValueError):r.generate(Path('/tmp'),Path('/tmp'),'test')
