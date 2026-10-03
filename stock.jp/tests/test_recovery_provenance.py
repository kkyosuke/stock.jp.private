"""Real Git/archive binding tests; domain validators have separate fixture tests."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from scripts import recovery_provenance as p


class ProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'source';self.root.mkdir()
        self.recovered=Path(self.temp.name)/'recovered'
        self.git('init','-q');self.git('config','user.name','Synthetic Test');self.git('config','user.email','test@example.invalid')
        private=self.root/'operations/private';private.mkdir(parents=True)
        (private/'state.json').write_text('{}')
        (private/'capital-ledger.csv').write_text('event_id\n')
        self.archive=private/'market-data';self.archive.mkdir();(self.archive/'daily-prices').mkdir();(self.archive/'daily-prices/observation.json').write_text('{"synthetic":true}')
        scripts=self.root/'stock.jp/scripts';scripts.mkdir(parents=True)
        for name in p.VALIDATORS:(scripts/name).write_text('# synthetic validator identity\n')
        p.codec.pack(self.archive,self.root/'stock.jp/data/jquants-archive')
        (self.root/'stock.jp/operations').mkdir()
        self.git('add','stock.jp','operations/private/state.json','operations/private/capital-ledger.csv')
        self.git('commit','-qm','synthetic inspected inputs')
        shutil.copytree(self.root,self.recovered)
        self.selected={'run_id':'2026-10-02','attempt':'1','artifact_hashes':{'handoff.json':'a'*64}}
        self.coverage={'eligible':True,'metrics':{'selected_run':self.selected}}
        self.details={'state_errors':[],'paper_go':True,'run_errors':[],'coverage':self.coverage}
        checks={key:True for key in ('clean_clone_single_repository','workspace_setup','state_validation','paper_bootstrap_check','latest_successful_run','latest_handoff','all_ledgers','unreconciled_orders')}
        self.proof=p.capture(self.root,self.recovered,self.selected,checks,self.details)
        m=self.proof['manifest']
        self.drill={'provenance':self.proof,'source_private_commit':m['commit'],'recovered_private_commit':m['commit'],
                    'source_code_tree':m['code_tree'],'recovered_code_tree':m['code_tree'],'checks':m['checks'],
                    'selected_run':self.selected,'completed_at_jst':'2026-10-03T12:00:00+09:00'}
    def git(self,*args):return subprocess.check_output(['git','-C',str(self.root),*args],stderr=subprocess.DEVNULL)
    def verify(self,drill=None):
        with patch('scripts.operation_state.validate_workspace',return_value=[]),patch('scripts.operation_bootstrap.check_readiness',return_value={'paper_go':True}),patch('scripts.recovery_drill.validated_paper_run',return_value=(self.coverage,self.selected,True,[])):
            return p.verify(self.root,drill or self.drill)
    def test_immutable_inspected_inputs_survive_later_daily_commit(self):
        result=self.verify();self.assertEqual(result['selected_run_id'],'2026-10-02')
        (self.root/'operations/private/state.json').write_text('{"later_daily_state":true}')
        self.git('add','operations/private/state.json');self.git('commit','-qm','later routine daily update')
        self.assertEqual(self.verify(),result)
    def test_recomputed_manifest_hash_does_not_allow_fabricated_inputs(self):
        for field in ('private_hashes','validators','archive_hashes'):
            with self.subTest(field=field):
                drill=copy.deepcopy(self.drill);m=drill['provenance']['manifest'];key=next(iter(m[field]));m[field][key]='0'*64
                drill['provenance']['sha256']=p.digest(p.encoded(m))
                with self.assertRaises(ValueError):self.verify(drill)
    def test_actual_historical_validation_must_agree(self):
        self.coverage={'eligible':False}
        with self.assertRaises(ValueError):self.verify()
    def test_capture_rejects_uncommitted_state_or_changed_archive(self):
        for path in (self.recovered/'operations/private/state.json',self.recovered/'operations/private/market-data/daily-prices/observation.json'):
            original=path.read_bytes();path.write_text('{"changed":true}')
            with self.assertRaises(ValueError):p.capture(self.root,self.recovered,self.selected,{'synthetic_check':True},self.details)
            path.write_bytes(original)

    def test_gate_accepts_verified_manifest_without_daily_age_expiry(self):
        from datetime import datetime
        from scripts.live_gate_evidence import evaluate_repository_recovery
        drill={**self.drill,'schema_version':'1.2','drill_id':'synthetic-recovery','operator':'synthetic-test',
               'repository_visibility':'PRIVATE','reason':'PRE_LIVE','repository_layout_revision':4,
               'started_at_jst':'2026-10-03T11:59:00+09:00','result_notes':'Synthetic test only'}
        path=self.root/'operations/private/evidence/recovery-drill.json';path.parent.mkdir();path.write_text(json.dumps(drill))
        with patch('scripts.operation_state.validate_workspace',return_value=[]),patch('scripts.operation_bootstrap.check_readiness',return_value={'paper_go':True}),patch('scripts.recovery_drill.validated_paper_run',return_value=(self.coverage,self.selected,True,[])):
            result=evaluate_repository_recovery(root=self.root,at=datetime.fromisoformat('2026-12-15T09:00:00+09:00'))
        self.assertTrue(result['eligible'],result['blockers'])

    def test_historical_bootstrap_cannot_create_missing_evidence(self):
        def mutate(*,root,**kwargs):
            (root/'operations/private/new-file.json').write_text('{}')
            return {'paper_go':True}
        with patch('scripts.operation_state.validate_workspace',return_value=[]),patch('scripts.operation_bootstrap.check_readiness',side_effect=mutate):
            with self.assertRaisesRegex(ValueError,'mutation'):p.verify(self.root,self.drill)
