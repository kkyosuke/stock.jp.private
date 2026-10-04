"""Synthetic GitHub REST + provider transactions, including irreversible faults."""
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from scripts import actions_date_grants as g, actions_date_git as remote, actions_date_authorization as a
from scripts import actions_date_driver as driver, actions_daily_remote as r, actions_http as http
from scripts import market_data_git as codec, actions_authorization as old_auth

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026,10,4,12,tzinfo=timezone.utc)
TARGET = '2025-08-07'


class Server:
    def __init__(self):
        self.objects, self.refs, self.prs, self.calls = {}, {}, [], []
        self.fail_write = self.lose_ref_ack = False
        self.context = dict(repository=r.REPO, ref='refs/heads/main', event='workflow_dispatch',
                            actor='kkyosuke', triggering_actor='kkyosuke', run_id=100, run_attempt=1)
        previous = (ROOT/old_auth.MANIFEST).read_bytes()
        receipt = (ROOT/'.github/stock-automation-retirement.json').read_bytes()
        legacy = json.loads(previous)
        self.seed = dict(version=1, legacy_debit=legacy['legacy_debit'], prior_charges={'2026-10-02':12}, prior_batches=1, grants=[])
        index = codec.encoded(dict(files={'daily-prices/2026/2026-10-02.csv':{}}))
        index_sha = codec.digest(index)
        files = {g.LEDGER:codec.encoded(self.seed), old_auth.MANIFEST:previous,
                 '.github/stock-automation-retirement.json':receipt,
                 'operations/private/source-config.json':(ROOT/'operations/private/source-config.json').read_bytes(),
                 'operations/private/operation-policy.json':codec.encoded(dict(operation_mode='PAPER',broker_submission='HUMAN_ONLY')),
                 'stock.jp/data/jquants-archive/current.json':codec.encoded(dict(version=2,generation=index_sha)),
                 'stock.jp/data/jquants-archive/generations/'+index_sha[:2]+'/'+index_sha+'.json':index}
        baseline = self.commit(files, [])
        self.scope = dict(version=1, enabled=True, writer='date-grant-v1', repository=r.REPO,
            workflow=a.WORKFLOW, assurance='retired-external-cooperative-github', data_publication_authorized=True,
            reservation_authorized=True, mode='PAPER', submission='HUMAN_ONLY', plan='light', addons=[], daily_limit=12,
            history_limits=[280,600,50,12], price_coverage_min=.98, active_coverage_min=1., expires_at='2026-10-11T00:00:00+00:00',
            previous_approval_sha256=codec.digest(previous), approval_pr=99, retirement_sha256=codec.digest(receipt),
            activation_id=legacy['activation_id'], baseline=dict(main_sha=baseline,ledger_sha256=codec.digest(files[g.LEDGER])))
        self.pin = codec.digest(codec.encoded(self.scope))
        files[a.MANIFEST] = codec.encoded(self.scope)
        self.refs['main'] = self.commit(files,[baseline])
        self.context['sha'] = self.refs['main']
        self.provider_calls = []

    def put(self, obj):
        digest = hashlib.sha1(codec.encoded(obj)).hexdigest(); self.objects[digest] = copy.deepcopy(obj); return digest

    def blob(self, raw):
        digest = r.git_blob(raw)
        self.objects[digest] = dict(content=base64.b64encode(raw).decode(),size=len(raw),encoding='base64')
        return digest

    def commit(self, files, parents):
        tree = self.put(dict(tree=[dict(path=p,mode='100644',type='blob',sha=self.blob(b),size=len(b)) for p,b in sorted(files.items())],truncated=False))
        return self.put(dict(tree={'sha':tree},parents=[{'sha':p} for p in parents]))

    def read(self, branch, path):
        tree = old_auth.complete_tree(self,self.refs[branch])
        return remote.read(self,tree,path)

    def adopt(self, branch):
        head = self.refs[branch]
        self.refs['main'] = self.put(dict(tree=self.objects[head]['tree'],parents=[{'sha':self.refs['main']},{'sha':head}]))
        for p in self.prs:
            if p['head']['ref'] == branch: p['state'] = 'closed'

    def provider(self, endpoint, query):
        self.provider_calls.append((endpoint,copy.deepcopy(query)))
        return dict(data=[dict(Date=TARGET,HolDiv='1')] if endpoint == '/markets/calendar' else [dict(Date=TARGET,Code='12340',C=123.45)])

    def __call__(self, method, path, body=None):
        self.calls.append((method,path,copy.deepcopy(body))); suffix=path.removeprefix(remote.PREFIX)
        if method != 'GET' and self.fail_write: raise r.RemoteStop('synthetic write failure')
        if method == 'GET':
            if suffix == '': return dict(full_name=r.REPO,private=True)
            if suffix.startswith('/git/ref/heads/'):
                branch=suffix.removeprefix('/git/ref/heads/')
                if branch not in self.refs: raise http.Rejected(404)
                return dict(object=dict(sha=self.refs[branch]))
            if suffix.startswith('/contents/'):
                path, commit = suffix.removeprefix('/contents/').split('?ref=')
                tree = old_auth.complete_tree(self,commit)
                return dict(type='file',encoding='base64',content=base64.b64encode(remote.read(self,tree,path)).decode())
            if suffix.startswith('/git/'):
                ident=suffix.split('?')[0].rsplit('/',1)[1]; return dict(self.objects[ident],sha=ident)
            if suffix.startswith('/actions/runs/'):
                attempt=int(suffix.rsplit('/',1)[1])
                return dict(id=100,run_attempt=attempt,head_sha=self.context['sha'],head_branch='main',path=a.WORKFLOW,
                    event='workflow_dispatch',repository=dict(full_name=r.REPO),actor=dict(login='kkyosuke'),triggering_actor=dict(login='kkyosuke'),
                    status='in_progress' if attempt==self.context['run_attempt'] else 'completed')
            if suffix == '/pulls/99': return dict(merged=True,base=dict(ref='main',repo=dict(full_name=r.REPO)))
            if suffix.startswith('/issues/99/comments'): return [dict(id=1,user=dict(login='kkyosuke'),body='APPROVE_STOCK_AUTOMATION sha256='+self.pin)]
            if suffix.startswith('/pulls?'): return copy.deepcopy(self.prs)
        if suffix == '/git/blobs': return dict(sha=self.blob(base64.b64decode(body['content'])))
        if suffix == '/git/trees':
            entries={e['path']:e for e in self.objects[body['base_tree']]['tree']}; entries.update({e['path']:{**e,'size':self.objects[e['sha']]['size']} for e in body['tree']})
            return dict(sha=self.put(dict(tree=list(entries.values()),truncated=False)))
        if suffix == '/git/commits': return dict(sha=self.put(dict(tree=dict(sha=body['tree']),parents=[dict(sha=p) for p in body['parents']])))
        if suffix == '/git/refs' or method == 'PATCH':
            branch=body['ref'].removeprefix('refs/heads/') if method=='POST' else suffix.removeprefix('/git/refs/heads/')
            if method=='POST':
                if branch in self.refs: raise http.Rejected(422)
            else:
                assert body['force'] is False
                assert self.objects[body['sha']]['parents'][0]['sha'] == self.refs[branch]
            self.refs[branch]=body['sha']
            if self.lose_ref_ack: raise r.RemoteStop('synthetic acknowledgement lost')
            return {}
        if suffix == '/pulls':
            p=dict(id=len(self.prs)+1,number=len(self.prs)+1,state='open',html_url='https://synthetic.invalid/pr/'+str(len(self.prs)+1),
                   head=dict(ref=body['head'],sha=self.refs[body['head']],repo=dict(full_name=r.REPO)))
            self.prs.append(p);return p
        raise AssertionError((method,suffix))

    def execute(self):
        def local_git(root,*args):
            if args==('rev-parse','HEAD'): return self.refs['main'].encode()
            if args==('status','--porcelain'): return b''
            if args==('remote','get-url','origin'): return ('https://github.com/'+r.REPO+'.git').encode()
            raise AssertionError(args)
        with patch.object(driver,'git',side_effect=local_git):
            return driver.Driver('.',self,self.provider,self.context,self.pin,clock=lambda:NOW,sleep=lambda _:None).execute(TARGET)

    def reserve(self):
        result=self.execute(); assert result['status']=='WAIT_RESERVATION_MERGE'
        self.adopt('date-acquisition/reserve/'+TARGET)
        self.context['run_attempt']=2


class DateTests(unittest.TestCase):
    def setUp(self): self.s=Server()

    def test_prepare_execute_restore_adopt_idempotent(self):
        self.s.reserve(); self.assertEqual(self.s.provider_calls,[])
        ledger=json.loads(self.s.read('main',g.LEDGER)); self.assertEqual(ledger['grants'][0]['allowance'],8)
        self.assertEqual(ledger['legacy_debit'],self.s.seed['legacy_debit'])
        result=self.s.execute(); self.assertEqual(result['provider_requests'],3)
        self.s.adopt('date-acquisition/result/'+TARGET)
        self.s.context['run_attempt']=3
        self.assertEqual(self.s.execute()['status'],'ALREADY_ADOPTED')
        self.assertEqual(len(self.s.provider_calls),3)
        self.assertEqual(json.loads(self.s.read('main',g.LEDGER)),ledger)
        self.assertFalse(any('checkpoint' in path for _,path,_ in self.s.calls))

    def test_unmerged_reservation_never_sends(self):
        self.s.execute(); self.assertEqual(self.s.execute()['status'],'WAIT_RESERVATION_MERGE')
        self.assertEqual(self.s.provider_calls,[])

    def test_deleted_result_cannot_reset_grant(self):
        self.s.reserve(); self.s.execute()
        del self.s.refs['date-acquisition/result/'+TARGET]
        self.s.context['run_attempt']=3
        with self.assertRaises(r.RemoteStop): self.s.execute()
        self.assertEqual(len(self.s.provider_calls),3)
        self.assertEqual(len(json.loads(self.s.read('main',g.LEDGER))['grants']),1)

    def test_missing_result_without_pr_cannot_replay_another_attempt(self):
        self.s.reserve();self.s.context['run_attempt']=3
        with self.assertRaisesRegex(r.RemoteStop,'one-use'):self.s.execute()
        self.assertEqual(self.s.provider_calls,[])

    def test_unknown_provider_response_remains_charged_and_blocks_resume(self):
        self.s.reserve()
        def unknown(*args): self.s.provider_calls.append(args);raise TimeoutError('do not log')
        self.s.provider=unknown
        with self.assertRaises(r.RemoteStop):self.s.execute()
        saved=json.loads(self.s.read('date-acquisition/result/'+TARGET,g.ROOT+TARGET+'/result.json'))
        self.assertEqual(saved['requests'][0]['outcome'],'FAILED_OR_UNKNOWN')
        self.s.context['run_attempt']=3
        with self.assertRaises(r.RemoteStop):self.s.execute()
        self.assertEqual(len(self.s.provider_calls),1)

    def test_unknown_durable_write_before_send_has_zero_provider_calls(self):
        self.s.reserve();self.s.lose_ref_ack=True
        with self.assertRaises(r.RemoteStop):self.s.execute()
        self.assertEqual(self.s.provider_calls,[])
        self.s.lose_ref_ack=False;self.s.context['run_attempt']=3
        with self.assertRaises(r.RemoteStop):self.s.execute()
        self.assertEqual(self.s.provider_calls,[])

    def test_completed_branch_can_resume_only_publication(self):
        self.s.reserve();self.s.execute();self.s.context['run_attempt']=3
        self.assertEqual(self.s.execute()['provider_requests'],0)
        self.assertEqual(len(self.s.provider_calls),3)

    def test_bad_pin_owner_expiry_and_modified_previous_scope_stop(self):
        for mutation in ('pin','owner','expiry','previous'):
            with self.subTest(mutation=mutation):
                s=Server()
                if mutation=='pin':s.pin='0'*64
                elif mutation=='owner':s.context['actor']='other'
                elif mutation=='expiry':
                    with patch.object(a,'datetime') as clock:
                        clock.fromisoformat.return_value=NOW
                        with self.assertRaises(r.RemoteStop):s.execute()
                    continue
                else:
                    tree=old_auth.complete_tree(s,s.refs['main'])
                    files={p:remote.read(s,tree,p) for p in tree};files[old_auth.MANIFEST]=b'{}'
                    s.refs['main']=s.commit(files,[s.refs['main']])
                with self.assertRaises(r.RemoteStop):s.execute()
                self.assertEqual(s.provider_calls,[])

    def test_main_ledger_deletion_in_history_is_detected(self):
        self.s.reserve()
        tree=old_auth.complete_tree(self.s,self.s.refs['main'])
        files={p:remote.read(self.s,tree,p) for p in tree};files[g.LEDGER]=codec.encoded(self.s.seed)
        self.s.refs['main']=self.s.commit(files,[self.s.refs['main']])
        with self.assertRaisesRegex(r.RemoteStop,'removed or rewritten'):self.s.execute()
        self.assertEqual(self.s.provider_calls,[])

    def test_page_reservation_is_durable_before_each_send_and_cap(self):
        self.s.reserve()
        def paged(endpoint,query):
            stored=json.loads(self.s.read('date-acquisition/result/'+TARGET,g.ROOT+TARGET+'/result.json'))
            self.assertEqual(stored['requests'][-1]['outcome'],'PENDING')
            self.s.provider_calls.append((endpoint,query))
            return dict(data=[dict(Date=TARGET,HolDiv='1')],pagination_key=str(len(self.s.provider_calls)))
        self.s.provider=paged
        with self.assertRaisesRegex(r.RemoteStop,'allowance exhausted'):self.s.execute()
        self.assertEqual(len(self.s.provider_calls),8)

    def test_tampered_response_and_non_data_candidate_fail(self):
        self.s.reserve();self.s.execute()
        branch='date-acquisition/result/'+TARGET
        tree=old_auth.complete_tree(self.s,self.s.refs[branch])
        files={p:remote.read(self.s,tree,p) for p in tree};files[g.ROOT+TARGET+'/equities_daily/001.json']=b'{}'
        self.s.refs[branch]=self.s.commit(files,[self.s.refs[branch]])
        with self.assertRaisesRegex(r.RemoteStop,'hash mismatch'):self.s.execute()
        self.assertEqual(len(self.s.provider_calls),3)

    def test_store_budget_prevents_send(self):
        self.s.reserve()
        with patch.object(codec,'MAX_STORE',1):
            with self.assertRaisesRegex(r.RemoteStop,'store size'):self.s.execute()
        self.assertEqual(self.s.provider_calls,[])

    def test_changed_owner_between_reservation_and_send(self):
        self.s.reserve()
        real_run=a.run
        count=0
        def observe(request, context):
            nonlocal count
            count+=1
            if count>=3:raise r.RemoteStop('owner changed')
            return real_run(request,context)
        with patch.object(a,'run',side_effect=observe):
            with self.assertRaisesRegex(r.RemoteStop,'owner changed'):self.s.execute()
        self.assertEqual(self.s.provider_calls,[])

    def test_page_roundtrip_and_holiday(self):
        self.s.reserve()
        def paged(endpoint,query):
            self.s.provider_calls.append((endpoint,copy.deepcopy(query)))
            if endpoint=='/markets/calendar':return dict(data=[dict(Date=TARGET,HolDiv='1')])
            if 'pagination_key' not in query:return dict(data=[dict(Date=TARGET,Code='12340')],pagination_key='page2')
            return dict(data=[dict(Date=TARGET,Code='56780')])
        self.s.provider=paged
        self.assertEqual(self.s.execute()['provider_requests'],5)
        self.s.adopt('date-acquisition/result/'+TARGET)
        self.assertEqual(self.s.execute()['status'],'ALREADY_ADOPTED')
        s=Server();s.reserve()
        s.provider=lambda *_:dict(data=[dict(Date=TARGET,HolDiv='0')])
        self.assertEqual(s.execute()['provider_requests'],1)

    def test_adopted_data_mutation_and_extra_code_in_result_pr_stop(self):
        self.s.reserve();self.s.execute()
        branch='date-acquisition/result/'+TARGET
        tree=old_auth.complete_tree(self.s,self.s.refs[branch])
        files={p:remote.read(self.s,tree,p) for p in tree};files['evil.py']=b'print(1)'
        self.s.refs[branch]=self.s.commit(files,[self.s.refs[branch]])
        with self.assertRaisesRegex(r.RemoteStop,'additive date data PR'):self.s.execute()
        self.assertEqual(len(self.s.provider_calls),3)

    def test_limits_and_no_refund(self):
        ledger,grant=g.propose(self.s.seed,TARGET,self.s.context)
        self.assertEqual(grant['allowance'],8)
        with self.assertRaises(r.RemoteStop):g.propose(ledger,TARGET,self.s.context)
        changed=copy.deepcopy(ledger);changed['grants'][0]['allowance']=7
        with self.assertRaises(r.RemoteStop):g.extends(ledger,changed)
        changed=copy.deepcopy(ledger);changed['grants'][0]['allowance']=9
        with self.assertRaises(r.RemoteStop):g.validate(changed)

    def test_light_history_window_and_future_dates(self):
        from datetime import date
        campaign=self.s.seed['legacy_debit']['campaign']
        g.check_target(TARGET,campaign,NOW.date())
        with self.assertRaisesRegex(r.RemoteStop,'Light history'):g.check_target(TARGET,campaign,date(2031,1,1))
        with self.assertRaisesRegex(r.RemoteStop,'reviewed campaign'):g.check_target('2027-01-01',campaign,NOW.date())

    def test_campaign_total_and_batch_limits(self):
        from datetime import date, timedelta
        value=copy.deepcopy(self.s.seed)
        for index in range(48):
            value['grants'].append(dict(date=(date(2025,9,1)+timedelta(days=index)).isoformat(),
                run_id=100+index,attempt=2,head_sha='a'*40,allowance=12))
        g.validate(value)  # 24 existing + 48*12 = 600, 2 existing + 48 = 50.
        value['grants'].append(dict(date='2025-11-01',run_id=999,attempt=2,head_sha='a'*40,allowance=1))
        with self.assertRaisesRegex(r.RemoteStop,'request limit'):g.validate(value)
        value['grants'][0]['allowance']=1
        with self.assertRaisesRegex(r.RemoteStop,'batch limit'):g.validate(value)

    def test_owner_approval_and_current_run_evidence_are_required(self):
        for missing in ('approval','run'):
            s=Server(); original=s.__call__
            def request(method,path,body=None):
                if missing=='approval' and '/comments?' in path:return []
                result=original(method,path,body)
                if missing=='run' and '/actions/runs/' in path:result['status']='completed'
                return result
            with self.assertRaises(r.RemoteStop):a.verify(request,s.context,s.pin,NOW)
            self.assertFalse(any(m!='GET' for m,_,_ in s.calls))

    def test_corrupt_policy_or_cost_seed_is_not_accepted(self):
        for key in ('legacy_debit','prior_charges'):
            seed=copy.deepcopy(self.s.seed)
            if key=='legacy_debit':seed[key]['batches']=[]
            else:seed[key]={'2026-10-02':13}
            with self.assertRaises(r.RemoteStop):g.validate(seed)

    def test_transport_rejects_main_force_merge_and_old_checkpoint(self):
        transport=remote.Transport('synthetic')
        for method,path,body in [('PATCH','/git/refs/heads/main',dict(force=False)),
            ('PATCH','/git/refs/heads/date-acquisition/result/'+TARGET,dict(force=True)),
            ('POST','/git/refs',dict(ref='refs/heads/automation/daily-checkpoint-v1')),
            ('PUT','/pulls/1/merge',{})]:
            with self.assertRaises(r.RemoteStop):transport(method,remote.PREFIX+path,body)


if __name__=='__main__':unittest.main()
