from tests.operation_test_support import prepare_write
from stock_jp.workspace import project_root
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import stock_jp.automation.pr_validation as p
import stock_jp.market_data.storage as codec


class DailyGateTest(unittest.TestCase):
    def proof(self):
        return dict(validated=True, repository=p.REPOS['market'], kind='market', run_id='2026-10-02',
                    base_sha='a'*40, head_sha='b'*40, paths=['data/market/jquants/current.json'],
                    validated_at=datetime.now(timezone.utc).isoformat())

    def evidence(self):
        proof = self.proof()
        return dict(fetched_at=datetime.now(timezone.utc).isoformat(), repository=proof['repository'],
                    visibility='private', auto_merge_allowed=True, main_sha=proof['base_sha'],
                    pr=dict(number=123, state='open', draft=False, merged=False, base_ref='main',
                            head_repository=proof['repository'], author_login='kkyosuke',
                            head_ref='daily/market/2026-10-02', head_sha=proof['head_sha'],
                            base_sha=proof['base_sha'], mergeable=True, review_decision=None),
                    checks_complete=True, files_complete=True, statuses_complete=True, runs_complete=True, reviews_complete=True,
                    changed_paths=proof['paths'], statuses=[],
                    workflow_runs=[dict(path=path, head_sha=proof['head_sha'],status='completed',conclusion='success',event='pull_request') for path in p.WORKFLOWS['market']],
                    checks=[dict(name=n, head_sha=proof['head_sha'], status='completed',conclusion='success',app_slug='github-actions') for n in p.CHECKS['market']])

    def test_validated_non_draft_daily_pr_allowed(self):
        self.assertEqual(p.allow_auto_merge(self.proof(),self.evidence())['action'],'enable_native_auto_merge')

    def test_failed_history_requires_review(self):
        for status in ('PAUSED','BLOCKED','EXHAUSTED','RUNNING'):
            proof=self.proof();proof['history_status']=status
            with self.subTest(status=status),self.assertRaises(p.Blocked):p.allow_auto_merge(proof,self.evidence())
        for status in ('COMPLETE','BATCH_EXHAUSTED'):
            proof=self.proof();proof['history_status']=status
            self.assertTrue(p.allow_auto_merge(proof,self.evidence())['eligible'])

    def test_identity_visibility_reviews_and_head_base_races_block(self):
        for field,value in [('draft',True),('head_sha','c'*40),('base_sha','c'*40),('head_ref','dependabot/pkg'),('author_login','other'),('review_decision','CHANGES_REQUESTED'),('mergeable',False)]:
            e=self.evidence();e['pr'][field]=value
            with self.subTest(field=field),self.assertRaises(p.Blocked):p.allow_auto_merge(self.proof(),e)
        for field,value in [('visibility','public'),('main_sha','c'*40),('auto_merge_allowed',False),('files_complete',False),('runs_complete',False),('changed_paths',['scripts/evil.py'])]:
            e=self.evidence();e[field]=value
            with self.subTest(field=field),self.assertRaises(p.Blocked):p.allow_auto_merge(self.proof(),e)

    def test_pending_failed_skipped_old_or_spoofed_checks_block(self):
        for field,value in [('status','queued'),('conclusion','failure'),('conclusion','skipped'),('head_sha','c'*40),('app_slug','other')]:
            e=self.evidence();e['checks'][0][field]=value
            with self.subTest(value=value),self.assertRaises(p.Blocked):p.allow_auto_merge(self.proof(),e)
        for field,value in [('checks',[]),('workflow_runs',[]),('statuses',[{'sha':'b'*40,'state':'pending'}])]:
            e=self.evidence();e[field]=value
            with self.assertRaises(p.Blocked):p.allow_auto_merge(self.proof(),e)
        e=self.evidence();e['workflow_runs'][0]['status']='in_progress'
        with self.assertRaises(p.Blocked):p.allow_auto_merge(self.proof(),e)
        e=self.evidence();e['fetched_at']=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
        with self.assertRaises(p.Blocked):p.allow_auto_merge(self.proof(),e)

    def test_allowlists_reject_configuration_financial_ledger_and_prior_run(self):
        for kind,path in [('market','scripts/x.py'),('market','data/market/legacy-prices/latest.json'),
                          ('personal','config/policy.json'),
                          ('personal','config/sources.json'),
                          ('personal','operations/ledgers/capital-ledger.csv'),
                          ('personal','operations/runs/2026-10-01/report.md')]:
            with self.subTest(path=path),self.assertRaises(p.Blocked):p.classify(kind,'2026-10-02',[{'path':path,'status':'M'}])
        with self.assertRaises(p.Blocked):p.classify('market','2026-10-02',[{'path':'data/market/jquants/objects/aa/' + 'a' * 64 + '.gz','status':'M'}])

    def test_real_git_partial_archive_passes_but_false_completeness_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';(source/'jquants').mkdir(parents=True)
            names=('equities_master','equities_daily','equities_valuation','financial_summary','earnings_dates','earnings_calendar','trading_calendar','investor_types','topix_daily')
            report={'provider':'J-Quants API V2','declared_plan':'light','declared_addons':[], 'all_entitled_complete':False,
                    'datasets':{n:{'status':'supported','collection_status':'pending_resume'} for n in names}}
            (prepare_write(source/'jquants/latest.json')).write_text(json.dumps(report))
            def git(*args):return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL).decode().strip()
            git('init');git('config','user.email','synthetic@example.test');git('config','user.name','Synthetic')
            git('commit','--allow-empty','-m','base');base=git('rev-parse','HEAD')
            codec.pack(source,root / 'data/market/jquants');git('add','data/market');git('commit','-m','sample');head=git('rev-parse','HEAD')
            proof=p.validate_local(root,'market','2026-10-02',base,head)
            self.assertEqual(proof['scope'],'partial_acquisition');self.assertFalse(proof['operational_readiness'])
            base=head
            report['all_entitled_complete']=True;(prepare_write(source/'jquants/latest.json')).write_text(json.dumps(report))
            codec.pack(source,root / 'data/market/jquants');git('add','data/market');git('commit','-m','bad');head=git('rev-parse','HEAD')
            with self.assertRaises(p.Blocked):p.validate_local(root,'market','2026-10-02',base,head)


if __name__=='__main__':unittest.main()

class ProposalEventTest(unittest.TestCase):
    def test_matching_virtual_proposal_allowed_real_event_or_rewrite_rejected(self):
        import csv,io
        fields=['event_id','occurred_at_jst','trade_date','ticket_id','decision_id','code','side','event_type','operation_mode','rule_version','order_type','limit_price','quantity_private','evidence_path','notes','fees']
        order=dict(ticket_id='synthetic',prepared_at_jst='2026-10-02T21:00:00+09:00',trade_date='2026-10-05',decision_id='synthetic',code='1234',side='BUY',rule_version='v0.4',order_type='LIMIT',limit_price='100',quantity_private='100',status='PAPER_PROPOSED')
        event={k:order.get(k,'') for k in fields};event.update(event_id='synthetic-PAPER_PROPOSED',occurred_at_jst=order['prepared_at_jst'],event_type='PAPER_PROPOSED',operation_mode='PAPER',evidence_path='operations/runs/2026-10-02/orders.csv')
        out=io.StringIO();w=csv.DictWriter(out,fieldnames=fields,lineterminator='\n');w.writeheader();old=out.getvalue().encode();w.writerow(event);new=out.getvalue().encode()
        p.validate_proposal_events(old,new,[order],'2026-10-02')
        for field,value in [('event_type','FILLED'),('operation_mode','LIVE'),('fees','10'),('quantity_private','200'),('ticket_id','unknown')]:
            changed=dict(event);changed[field]=value;out=io.StringIO();w=csv.DictWriter(out,fieldnames=fields,lineterminator='\n');w.writerow(changed)
            with self.assertRaises(p.Blocked):p.validate_proposal_events(old,old+out.getvalue().encode(),[order],'2026-10-02')
        with self.assertRaises(p.Blocked):p.validate_proposal_events(old,b'changed-history',[order],'2026-10-02')

class ArchiveReachabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=self.root/'store';self.source=self.root/'source'
        (self.source/'jquants').mkdir(parents=True)
        (prepare_write(self.source/'jquants/latest.json')).write_text('{"synthetic":"first"}')
        codec.pack(self.source,self.store)

    def additions(self, before=()):
        return [{'path':'data/market/jquants/' + p.relative_to(self.store).as_posix(),'status':'A'}
                for p in self.store.rglob('*') if p.is_file() and p.name != 'current.json' and p not in before]

    def test_unreferenced_valid_hash_secret_object_or_generation_rejected(self):
        for kind in ('objects','generations'):
            with self.subTest(kind=kind):
                sha=codec.put(self.store,b'{"api_key":"synthetic-hidden","personal_analysis":"synthetic"}',kind)
                with self.assertRaisesRegex(p.Blocked,'unreferenced'):
                    p.validate_added_archive_members(self.store,self.additions())
                codec.object_path(self.store,sha,kind).unlink()

    def test_existing_history_preserved_legitimate_correction_validates(self):
        old={p for p in self.store.rglob('*') if p.is_file() and p.name != 'current.json'}
        (prepare_write(self.source/'jquants/latest.json')).write_text('{"synthetic":"correction"}')
        codec.pack(self.source,self.store)
        p.validate_added_archive_members(self.store,self.additions(old))
        self.assertTrue(codec.verify(self.store)['verified'])
        self.assertTrue(all(x.exists() for x in old))
        # A second generation added in the same PR is not silently accepted.
        with self.assertRaisesRegex(p.Blocked,'unreferenced'):
            p.validate_added_archive_members(self.store,self.additions())

    def test_extra_generation_metadata_and_duplicate_fields_rejected(self):
        pointer=json.loads((self.store/'current.json').read_text())
        data=codec.manifest_bytes(self.store,pointer)
        metadata=json.loads(data);metadata['personal_analysis']='hidden'
        pointer['generation']=Path(codec.put_plain(self.store,codec.encoded(metadata),'.json','generations')).stem
        (prepare_write(self.store/'current.json')).write_bytes(codec.encoded(pointer))
        with self.assertRaises(p.Blocked):p.validate_added_archive_members(self.store,self.additions())
        with self.assertRaises(p.Blocked):p.strict_metadata(b'{"files":{},"files":{}}')

    def test_referenced_corrupt_and_oversized_blobs_fail_validation(self):
        from unittest.mock import patch
        p.validate_added_archive_members(self.store,self.additions())
        obj=next((self.store/'objects').rglob('*.json'))
        original=obj.read_bytes();prepare_write(obj).write_bytes(b'not-gzip')
        with self.assertRaises(codec.ArchiveError):codec.verify(self.store)
        prepare_write(obj).write_bytes(original)
        with patch.object(codec,'MAX_SHARD',1),self.assertRaises(codec.ArchiveError):
            p.validate_added_archive_members(self.store,self.additions())
        with patch.object(codec,'MAX_INDEX',1),self.assertRaises(codec.ArchiveError):
            p.validate_added_archive_members(self.store,self.additions())

class PersonalStateTransitionTest(unittest.TestCase):
    def setUp(self):
        import shutil
        from stock_jp.operation.state import initialize_or_migrate_workspace
        from stock_jp.operation.runs import prepare_run
        from stock_jp.operation.smoke import _complete_artifacts
        from stock_jp.operation.nightly import finalize_nightly_run
        from datetime import date
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        shutil.copytree(project_root() / 'templates',self.root / 'templates')
        initialize_or_migrate_workspace(self.root)
        self.run_id='2026-10-02'
        self.prepare_market_fixture()
        self.git('init');self.git('config','user.email','synthetic@example.test');self.git('config','user.name','Synthetic')
        self.git('add','config','operations','data/market/observations');self.git('commit','-m','base');self.base=self.git('rev-parse','HEAD')
        prepared=prepare_run(at='2026-10-02T18:30:00+09:00',root=self.root)
        run=self.root/prepared['run_dir'];_complete_artifacts(run,date(2026,10,2))
        self.run_market_assessment()
        handoff=json.loads((run/'handoff.json').read_text())
        plan=json.loads((run/'work-plan.json').read_text())
        if plan['tasks']:
            task=plan['tasks'][0];task['status']='DEFERRED'
            handoff['pending_reviews']=[task['task_id']]
        else:
            handoff['pending_reviews']=['synthetic-pending-review']
        handoff['pending_orders']=['synthetic-open-ticket']
        (prepare_write(run/'handoff.json')).write_text(json.dumps(handoff));(prepare_write(run/'work-plan.json')).write_text(json.dumps(plan))
        finalize_nightly_run(run_id=self.run_id,run_token=prepared['run_token'],completed_at='2026-10-02T19:05:00+09:00',source_cutoff='2026-10-02T18:30:00+09:00',price_date=self.run_id,summary='synthetic completed run with pending work',root=self.root)
        self.git('add','operations');self.git('commit','-m','valid run');self.head=self.git('rev-parse','HEAD')
        self.state_path=self.root / 'operations/state.json'
        self.valid_state=json.loads(self.state_path.read_text())

    def prepare_market_fixture(self):
        pass

    def run_market_assessment(self):
        pass

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.root),*args],stderr=subprocess.DEVNULL).decode().strip()

    def test_real_completed_run_preserves_pending_work(self):
        proof=p.validate_local(self.root,'personal',self.run_id,self.base,self.head)
        self.assertEqual(proof['scope'],'completed_paper_results')
        self.assertTrue(self.valid_state['pending_reviews'])
        self.assertTrue(self.valid_state['pending_orders'])

    def test_each_poststate_mutation_is_rejected_end_to_end(self):
        for field,value in [('pending_reviews',[]),('pending_orders',[]),('unreconciled_ticket_ids',[]),
                            ('next_run_at_jst','2099-01-01T21:00:00+09:00'),('consecutive_successful_runs',999),
                            ('last_price_date','1999-01-01'),('data_gaps',[{'synthetic':'invented'}])]:
            with self.subTest(field=field):
                state=dict(self.valid_state);state[field]=value;prepare_write(self.state_path).write_text(json.dumps(state))
                self.git('add','operations/state.json');self.git('commit','-m','mutated '+field)
                with self.assertRaisesRegex(p.Blocked,'canonical completed-run transition'):
                    p.validate_local(self.root,'personal',self.run_id,self.base,self.git('rev-parse','HEAD'))


class GeneratedMarketRegimeTest(PersonalStateTransitionTest):
    def prepare_market_fixture(self):
        from datetime import date
        from stock_jp.operation.assessment import _append_mrs_log
        from stock_jp.strategy.regime import MarketRegimeInput, evaluate_market_regime
        self.archive = self.root / '.work/market-data/daily-prices'
        self.archive.mkdir(parents=True)
        (prepare_write(self.root / '.gitignore')).write_text('.work/market-data/\n')
        config_path = self.root / 'config/sources.json'
        config = json.loads(config_path.read_text())
        config['price_source']['storage'] = 'private_local'
        config['price_source']['archive_root'] = '.work/market-data/daily-prices'
        prepare_write(config_path).write_text(json.dumps(config))
        target = date(2026, 9, 30)
        points = []
        for offset in range(600):
            day = target - timedelta(days=599-offset)
            points.append({'date': day.isoformat(), 'value': 100 + offset})
            if offset >= 320:
                path = self.archive / str(day.year) / (day.isoformat() + '.csv')
                path.parent.mkdir(exist_ok=True, parents=True)
                prepare_write(path).write_text('銘柄コード,終値\n1234,' + str(100 + offset) + '\n')
        raw = {'schema_version': '1.0', 'as_of': target.isoformat(), 'series': {
            'topix': {'points': points, 'source_url': 'https://example.test/topix'},
            'growth250': {'points': points, 'source_url': 'https://example.test/growth'},
            'nikkei_vi': {'points': [dict(point, value=20) for point in points], 'source_url': 'https://example.test/vi'},
            'leading_ci': {'available_at_jst': '2026-09-01T08:50:00+09:00',
                           'points': [{'period':'2026-05','value':99},{'period':'2026-08','value':100}],
                           'source_url': 'https://example.test/ci'}}}
        self.fixture = self.root / 'fixtures'
        self.fixture.mkdir(parents=True)
        (prepare_write(self.fixture / 'market-regime-series.json')).write_text(json.dumps(raw))
        self.log = self.root / 'operations/ledgers/market-regime-log.csv'
        values = MarketRegimeInput('2026-08-31', 101, 100, 101, 100, 50, 20, 20, 101, 100)
        _append_mrs_log(self.log, values=values, result=evaluate_market_regime(values),
                        source_urls=['https://example.test/historical'], calculated_at='2026-09-01T18:30:00+09:00')
        self.old_log = self.log.read_bytes()

    def run_market_assessment(self):
        from stock_jp.operation.assessment import run_assessments
        result = run_assessments(run_id=self.run_id, at='2026-10-02T18:45:00+09:00',
                                 fixture_dir=self.fixture, root=self.root)
        self.assertEqual(result['market_regime_status'], 'CURRENT')

    def test_real_assessment_pipeline_outputs_validate(self):
        proof = p.validate_local(self.root, 'personal', self.run_id, self.base, self.head)
        self.assertEqual(proof['market_regime_evidence']['as_of'], '2026-09-30')
        self.assertTrue(self.log.read_bytes().startswith(self.old_log))
        self.assertEqual(len(proof['market_regime_evidence']['price_evidence_sha256']), 64)

    def test_historical_and_new_risk_log_edits_rejected(self):
        original = self.log.read_bytes()
        for before, after in [(b'https://example.test/historical', b'https://example.test/rewritten'),
                              (b'2026-10-02T18:45:00+09:00', b'2026-10-02T18:46:00+09:00')]:
            with self.subTest(field=before):
                prepare_write(self.log).write_bytes(original.replace(before, after))
                self.git('add', 'operations/ledgers/market-regime-log.csv')
                self.git('commit', '-m', 'tampered risk log')
                with self.assertRaisesRegex(p.Blocked, 'MRS (risk history|log differs)'):
                    p.validate_local(self.root, 'personal', self.run_id, self.base, self.git('rev-parse','HEAD'))

    def test_changed_price_evidence_changes_proof_and_threshold_override_rejected(self):
        proof = p.validate_local(self.root, 'personal', self.run_id, self.base, self.head)
        path = self.archive / '2026/2026-09-30.csv'
        prepare_write(path).write_text(path.read_text() + '\n')
        fresh = p.validate_local(self.root, 'personal', self.run_id, self.base, self.head)
        self.assertNotEqual(proof['market_regime_evidence'], fresh['market_regime_evidence'])
        input_path = self.root / 'operations/inputs/market-regime/2026-09-30.json'
        raw = json.loads(input_path.read_text());raw['minimum_breadth_coverage'] = 0
        prepare_write(input_path).write_text(json.dumps(raw));self.git('add', 'operations/inputs')
        self.git('commit', '-m', 'weaken threshold')
        with self.assertRaisesRegex(p.Blocked, 'override configured risk thresholds'):
            p.validate_local(self.root, 'personal', self.run_id, self.base, self.git('rev-parse','HEAD'))

class SingleRepositoryBoundaryTest(unittest.TestCase):
    def test_market_and_personal_paths_cannot_mix_or_use_the_old_root(self):
        pointer = {'path': 'data/market/jquants/current.json', 'status': 'M'}
        p.classify('market', '2026-10-02', [pointer])
        for path in ('stock.jp/data/market/jquants/current.json', 'stockXjp/data/market/jquants/current.json',
                     'operations/state.json', 'src/stock_jp/automation/pr_validation.py'):
            with self.subTest(path=path), self.assertRaises(p.Blocked):
                p.classify('market', '2026-10-02', [pointer, {'path': path, 'status': 'M'}])
        with self.assertRaises(p.Blocked):
            p.classify('personal', '2026-10-02', [pointer])
        self.assertEqual(set(p.REPOS.values()), {'kkyosuke/stock.jp.private'})
        for checks in p.CHECKS.values():
            self.assertEqual(checks, {'format', 'test', 'guardrail'})

    def test_old_repository_and_missing_private_checks_cannot_enable_auto_merge(self):
        fixtures = DailyGateTest()
        e = fixtures.evidence()
        e['repository'] = 'kkyosuke/stock.jp'
        with self.assertRaises(p.Blocked):
            p.allow_auto_merge(fixtures.proof(), e)
        e = fixtures.evidence()
        e['checks'] = [c for c in e['checks'] if c['name'] != 'format']
        with self.assertRaises(p.Blocked):
            p.allow_auto_merge(fixtures.proof(), e)

    def test_validator_rejects_nested_git_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'stock.jp').mkdir(parents=True)
            with self.assertRaisesRegex(p.Blocked, 'repository root'):
                p.validate_local(root / 'stock.jp', 'market', '2026-10-02', 'a'*40, 'b'*40)
