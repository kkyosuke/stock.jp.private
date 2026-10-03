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
        self.proof=p.capture(self.root,self.recovered,self.selected,checks,self.details,inspection_at_jst='2026-10-03T12:00:00+09:00')
        m=self.proof['manifest']
        self.drill={'provenance':self.proof,'source_private_commit':m['commit'],'recovered_private_commit':m['commit'],
                    'source_code_tree':m['code_tree'],'recovered_code_tree':m['code_tree'],'checks':m['checks'],
                    'selected_run':self.selected,'started_at_jst':'2026-10-03T12:00:00+09:00','completed_at_jst':'2026-10-03T12:00:00+09:00'}
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
            with self.assertRaises(ValueError):p.capture(self.root,self.recovered,self.selected,{'synthetic_check':True},self.details,inspection_at_jst=self.drill['started_at_jst'])
            path.write_bytes(original)

    def test_gate_accepts_verified_manifest_without_daily_age_expiry(self):
        from datetime import datetime
        from scripts.live_gate_evidence import evaluate_repository_recovery
        drill={**self.drill,'schema_version':'1.2','drill_id':'synthetic-recovery','operator':'synthetic-test',
               'repository_visibility':'PRIVATE','reason':'PRE_LIVE','repository_layout_revision':4,
               'started_at_jst':self.drill['started_at_jst'],'result_notes':'Synthetic test only'}
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

    def real_coverage_proof(self):
        """Exercise real coverage and run selection; only other validators are stubbed."""
        import csv
        from contextlib import ExitStack
        from datetime import datetime
        from tests.test_live_gate_evidence import OfficialCoverageEvidenceTest
        from scripts.recovery_drill import validated_paper_run
        fixture=Path(self.temp.name)/'coverage-fixture'
        OfficialCoverageEvidenceTest()._write_coverage(fixture)
        private=self.root/'operations/private'
        shutil.copytree(fixture/'operations/private',private,dirs_exist_ok=True)
        with (private/'run-history.csv').open() as stream:rows=list(csv.DictReader(stream))
        for row in rows:
            (private/'runs'/row['run_id']/'handoff.json').write_text(json.dumps({
                'run_id':row['run_id'],'attempt':int(row['attempt']),'status':'completed',
                'operation_mode':'PAPER','active_rule_version':'v0.4','completed_at_jst':row['completed_at_jst']}))
        (self.root/'stock.jp/operations/private').symlink_to(private,target_is_directory=True)
        p.codec.pack(self.archive,self.root/'stock.jp/data/jquants-archive')
        self.git('add','stock.jp/data','operations/private/run-history.csv','operations/private/runs',
                 'operations/private/source-config.json','operations/private/source-watermarks.json')
        self.git('commit','-qm','synthetic real coverage inputs')
        shutil.rmtree(self.recovered);shutil.copytree(self.root,self.recovered,symlinks=True)
        class Clock(datetime):
            instant=datetime.fromisoformat('2026-10-03T12:00:00+09:00')
            @classmethod
            def now(cls,tz=None):return cls.instant.astimezone(tz)
        context=self.enterContext(ExitStack())
        context.enter_context(patch('scripts.live_gate_evidence.datetime',Clock))
        context.enter_context(patch('scripts.live_gate_evidence.validate_run_artifacts'))
        context.enter_context(patch('scripts.recovery_drill.validate_run_artifacts'))
        context.enter_context(patch('scripts.operation_state.validate_workspace',return_value=[]))
        context.enter_context(patch('scripts.operation_bootstrap.check_readiness',return_value={'paper_go':True}))
        coverage,selected,ok,errors=validated_paper_run(self.root/'stock.jp')
        self.assertTrue(ok,(coverage,errors))
        proof=p.capture(self.root,self.recovered,selected,self.drill['checks'],
                        {'state_errors':[],'paper_go':True,'run_errors':errors,'coverage':coverage},inspection_at_jst=self.drill['started_at_jst'])
        m=proof['manifest'];drill={**self.drill,'provenance':proof,'selected_run':selected,
            'source_private_commit':m['commit'],'recovered_private_commit':m['commit'],
            'source_code_tree':m['code_tree'],'recovered_code_tree':m['code_tree']}
        return Clock,drill

    def test_real_coverage_revalidates_next_second_and_after_daily_commit(self):
        from datetime import datetime
        from scripts.recovery_drill import validated_paper_run
        clock,drill=self.real_coverage_proof()
        for instant in ('2026-10-03T12:00:01+09:00','2026-12-15T09:00:00+09:00'):
            with self.subTest(instant=instant):
                clock.instant=datetime.fromisoformat(instant)
                reevaluated=validated_paper_run(self.root/'stock.jp')[0]
                self.assertNotEqual(reevaluated['evaluated_at_jst'],drill['provenance']['manifest']['validation_details']['coverage']['evaluated_at_jst'])
                result=p.verify(self.root,drill)
                self.assertEqual(result['selected_run_id'],drill['selected_run']['run_id'])
                (self.root/'operations/private/state.json').write_text(json.dumps({'later_daily_update':instant}))
                self.git('add','operations/private/state.json');self.git('commit','-qm','later daily update')
                self.assertEqual(p.verify(self.root,drill),result)

    def test_real_coverage_clock_exclusion_keeps_tamper_detection(self):
        _,drill=self.real_coverage_proof()
        changed=copy.deepcopy(drill)
        changed['provenance']['manifest']['validation_details']['coverage']['evaluated_at_jst']='2026-10-03T12:00:01+09:00'
        with self.assertRaisesRegex(ValueError,'checksum'):p.verify(self.root,changed)
        for field,value in (('eligible',False),('blockers',['fabricated']),('metrics',{}),('inputs',[])):
            with self.subTest(field=field):
                changed=copy.deepcopy(drill);m=changed['provenance']['manifest']
                m['validation_details']['coverage'][field]=value
                changed['provenance']['sha256']=p.digest(p.encoded(m))
                with self.assertRaisesRegex(ValueError,'stored validation details differ'):p.verify(self.root,changed)

    def test_public_root_entrypoint_preserves_recorded_clock_and_code_identities(self):
        clock,drill=self.real_coverage_proof()
        drill['completed_at_jst']='2026-10-04T00:00:01+09:00'
        with patch('scripts.operation_bootstrap.check_readiness',return_value={'paper_go':True}) as bootstrap:
            result=p.verify(self.root/'stock.jp',drill)
        self.assertEqual(bootstrap.call_args.kwargs['at'],drill['started_at_jst'])
        self.assertEqual(result['inspection_at_jst'],drill['started_at_jst'])
        self.assertEqual(result['validation_code_mode'],'CURRENT_CODE_HISTORICAL_INPUTS')
        self.assertEqual(result['recorded_validator_hashes'],drill['provenance']['manifest']['validators'])
        self.assertNotEqual(result['recorded_validator_hashes'],result['revalidation_validator_hashes'])
        self.assertEqual(p.names(self.root,'HEAD','operations/private'),p.names(self.root/'stock.jp','HEAD','operations/private'))
        from scripts.live_gate_evidence import evaluate_repository_recovery
        drill.update(schema_version='1.2',drill_id='synthetic-public-root',operator='synthetic-test',
                     repository_visibility='PRIVATE',reason='PRE_LIVE',repository_layout_revision=4,result_notes='Synthetic only')
        path=self.root/'operations/private/evidence/recovery-drill.json';path.parent.mkdir();path.write_text(json.dumps(drill))
        from datetime import datetime
        result=evaluate_repository_recovery(root=self.root/'stock.jp',at=datetime.fromisoformat('2026-12-15T09:00:00+09:00'))
        self.assertTrue(result['eligible'],result['blockers'])
