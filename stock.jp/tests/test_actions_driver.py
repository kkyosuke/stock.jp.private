"""Real runners/codec/Git identities with synthetic GitHub and provider transports."""
import base64,copy,hashlib,json,subprocess,tempfile,unittest,zlib
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit,parse_qs
from scripts import actions_driver as d, actions_daily_remote as r, actions_publication as pub
from scripts import actions_authorization as auth, actions_attestation as attest, actions_http as http
from scripts import market_data_git as codec
from scripts.git_sandbox import git
import test_jquants_daily as df
import test_jquants_history as hf

class LocalGitHub:
    def __init__(self,root,main):
        self.root=root;self.refs={'heads/main':main};self.calls=[];self.prs={};self.loss=None
        self.reviews=[];self.ci_ready=True;self.merge_writes=0;self.store_bodies=True
    def obj(self,kind,raw):
        data=kind.encode()+b' '+str(len(raw)).encode()+b'\0'+raw
        sha=hashlib.sha1(data).hexdigest();p=self.root/'.git/objects'/sha[:2]/sha[2:]
        p.parent.mkdir(parents=True,exist_ok=True)
        if not p.exists():p.write_bytes(zlib.compress(data))
        return sha
    def tree(self,entries):
        nested={}
        for name,member in entries.items():
            parent=nested;parts=name.split('/')
            for part in parts[:-1]:parent=parent.setdefault(part,{})
            parent[parts[-1]]=member
        def build(items):
            rows=[]
            for name,value in items.items():
                if isinstance(value,tuple):mode,sha=value;key=name
                else:mode,sha='40000',build(value);key=name+'/'
                rows.append((key,mode.encode()+b' '+name.encode()+b'\0'+bytes.fromhex(sha)))
            return self.obj('tree',b''.join(raw for _,raw in sorted(rows)))
        return build(nested)
    def entries(self,sha):
        result={}
        for row in git(self.root,'ls-tree','-r','-z',sha).split(b'\0'):
            if not row:continue
            meta,name=row.split(b'\t');mode,kind,digest=meta.decode().split();result[name.decode()]=(mode,digest)
        return result
    def commit(self,tree,parents,message):
        raw='tree '+tree+'\n'+''.join('parent '+p+'\n' for p in parents)
        raw+='author Synthetic <test@invalid.example> 1791028800 +0000\ncommitter Synthetic <test@invalid.example> 1791028800 +0000\n\n'+message+'\n'
        return self.obj('commit',raw.encode())
    def review_decision(self,*args):return None
    def __call__(self,method,path,body=None):
        result=self.handle(method,path,body)
        if self.loss and self.loss(method,path):self.loss=None;raise r.RemoteStop('synthetic lost ACK')
        return result
    def handle(self,method,path,body):
        self.calls.append((method,path,copy.deepcopy(body) if self.store_bodies else None))
        parsed=urlsplit(path.removeprefix(pub.PREFIX));route=parsed.path;q=parse_qs(parsed.query)
        if route=='':return dict(full_name=r.REPO,private=True,allow_auto_merge=True)
        if route.startswith('/git/ref/'):
            ref=route.removeprefix('/git/ref/')
            if ref not in self.refs:raise http.Rejected(404)
            return {'object':{'sha':self.refs[ref]}}
        if route.startswith('/git/refs'):
            if method=='POST':
                ref=body['ref'].removeprefix('refs/')
                if ref in self.refs:raise http.Rejected(422)
            else:
                ref=route.removeprefix('/git/refs/')
                parents=git(self.root,'rev-list','--parents','-n','1',body['sha']).decode().split()[1:]
                if body.get('force') is not False or self.refs[ref] not in parents:raise http.Rejected(422)
            self.refs[ref]=body['sha'];return {'object':{'sha':body['sha']}}
        if route=='/git/blobs':return {'sha':self.obj('blob',base64.b64decode(body['content']))}
        if route.startswith('/git/blobs/'):
            raw=git(self.root,'cat-file','blob',route.rsplit('/',1)[1]);return dict(content=base64.b64encode(raw).decode(),size=len(raw),encoding='base64')
        if route=='/git/trees':
            entries=self.entries(body['base_tree']) if body.get('base_tree') else {}
            for row in body['tree']:entries[row['path']]=(row['mode'],row['sha'])
            return {'sha':self.tree(entries)}
        if route.startswith('/git/trees/'):
            return dict(truncated=False,tree=[dict(path=n,mode=m,type='blob',sha=s) for n,(m,s) in self.entries(route.rsplit('/',1)[1]).items()])
        if route=='/git/commits':return {'sha':self.commit(body['tree'],body['parents'],body['message'])}
        if route.startswith('/git/commits/'):
            sha=route.rsplit('/',1)[1];tree=git(self.root,'rev-parse',sha+'^{tree}').decode().strip()
            parents=git(self.root,'rev-list','--parents','-n','1',sha).decode().split()[1:]
            return dict(sha=sha,tree={'sha':tree},parents=[{'sha':p} for p in parents])
        if route.startswith('/contents/'):
            raw=git(self.root,'show',q['ref'][0]+':'+route.removeprefix('/contents/'))
            return dict(type='file',encoding='base64',content=base64.b64encode(raw).decode())
        if route.startswith('/compare/'):
            base,head=route.rsplit('/',1)[1].split('...');rows=git(self.root,'diff','--name-status',base,head).decode().splitlines()
            return dict(status='identical' if base==head else 'ahead',total_commits=1,
                files=[dict(filename=row.split('\t')[1],status='added' if row.startswith('A') else 'modified') for row in rows])
        if route=='/pulls/900':return dict(merged=True,base=dict(ref='main',repo={'full_name':r.REPO}))
        if route=='/issues/900/comments':return [dict(id=1,user={'login':'kkyosuke'},body='APPROVE_STOCK_AUTOMATION sha256='+self.pin)]
        if route=='/pulls':
            if method=='GET':return [copy.deepcopy(p) for p in self.prs.values() if q.get('state')!=['open'] or p['state']=='open']
            head=self.refs['heads/'+body['head']];number=len(self.prs)+1
            self.prs[number]=dict(id=number,number=number,state='open',draft=False,merged=False,mergeable=True,user={'login':'kkyosuke'},
                head=dict(sha=head,ref=body['head'],repo={'full_name':r.REPO}),
                base=dict(sha=self.refs['heads/main'],ref='main',repo={'full_name':r.REPO}))
            return {'number':number}
        if route.startswith('/pulls/'):
            parts=route.split('/');pr=self.prs[int(parts[2])]
            if len(parts)==3:return copy.deepcopy(pr)
            if parts[3]=='reviews':return self.reviews
            if parts[3]=='files':
                rows=[dict(filename=n) for n in git(self.root,'diff','--name-only',pr['base']['sha'],pr['head']['sha']).decode().splitlines()]
                size=int(q.get('per_page',['100'])[0]);start=(int(q.get('page',['1'])[0])-1)*size
                return rows[start:start+size]
            if parts[3]=='merge':
                assert body==dict(sha=pr['head']['sha'],merge_method='squash')
                assert self.refs['heads/main']==pr['base']['sha']
                tree=git(self.root,'rev-parse',pr['head']['sha']+'^{tree}').decode().strip()
                sha=self.commit(tree,[self.refs['heads/main']],'Synthetic normal merge')
                self.refs['heads/main']=sha;pr.update(merged=True,state='closed',merge_commit_sha=sha);self.merge_writes+=1
                return dict(merged=True,sha=sha)
        if '/check-runs' in route:
            head=route.split('/')[2]
            return dict(total_count=3,check_runs=[dict(id=i,name=n,head_sha=head,status='completed' if self.ci_ready else 'in_progress',
                conclusion='success' if self.ci_ready else None,app={'slug':'github-actions'}) for i,n in enumerate(('format','test','guardrail'))])
        if '/statuses' in route:return []
        if route=='/actions/runs':return dict(total_count=3,workflow_runs=[dict(id=i,head_sha=q['head_sha'][0],status='completed',conclusion='success',event='pull_request',path=p) for i,p in enumerate(pub.publication.WORKFLOWS['market'])])
        raise AssertionError((method,path))

class DriverTests(unittest.TestCase):
    def setUp(self):
        self.fixture=df.DailyTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root;self.source=self.fixture.private/'market-data';self.source.mkdir(exist_ok=True)
        (self.fixture.private/'operation-policy.json').write_text(json.dumps(dict(operation_mode='PAPER',broker_submission='HUMAN_ONLY')))
        codec.pack(self.source,self.root/'stock.jp/data/jquants-archive')
        git(self.root,'init','--quiet');git(self.root,'remote','add','origin','https://github.com/'+r.REPO+'.git')
        (self.root/'.gitignore').write_text('operations/private/market-data/\n')
        git(self.root,'add','.gitignore')
        git(self.root,'add','operations/private/source-config.json','operations/private/operation-policy.json','stock.jp/data')
        self.base=self.commit('Synthetic baseline');self.now=datetime.now(timezone.utc)
        self.fixture.at=datetime(2026,10,2,21,1,tzinfo=d.ZoneInfo('Asia/Tokyo'))
        receipt=dict(controller='chatgpt-automations',scheduler_id='synthetic-old',enabled=False,active_run_ids=[],
            next_writer='actions-v1',epoch='synthetic-epoch',observed_by='kkyosuke',observed_at=self.fixture.at.isoformat(),
            owner='synthetic-old-writer',lease_id='synthetic-lease-1',lease_state='released',lease_owner=None)
        self.controller_observation=copy.deepcopy(receipt)
        evidence=codec.encoded(receipt);receipt['source_evidence_sha256']=codec.digest(evidence);handover=codec.encoded(receipt)
        pointer=json.loads((self.root/'stock.jp/data/jquants-archive/current.json').read_bytes())
        self.scope=dict(version=1,enabled=True,repository=r.REPO,writer='actions-v1',modes=['daily','history'],
            publication_rejections=[],unresolved_campaigns=[],mode='PAPER',submission='HUMAN_ONLY',plan='light',addons=[],
            daily_limit=12,history_limits=[280,600,50,12],price_coverage_min=.98,active_coverage_min=1.,
            expires_at=(self.now+timedelta(days=1)).isoformat(),data_publication_authorized=True,checkpoint_authorized=True,
            old_scheduler_id='synthetic-old',old_writer='synthetic-old-writer',old_lease_id='synthetic-lease-1',baseline=dict(main_sha=self.base,generation=pointer['generation']),
            approval_pr=900,handover_sha256=codec.digest(handover),handover_epoch='synthetic-epoch',history_campaign={'as_of':'2026-10-02','sessions':8})
        codec.atomic(self.root/attest.HANDOVER,handover);codec.atomic(self.root/'.github/stock-automation-controller-evidence.json',evidence)
        codec.atomic(self.root/auth.MANIFEST,codec.encoded(self.scope));git(self.root,'add','.github')
        main=self.commit('Synthetic reviewed activation');self.remote=LocalGitHub(self.root,main);self.remote.pin=codec.digest(codec.encoded(self.scope))
        self.context=dict(repository=r.REPO,ref='refs/heads/main',event='workflow_dispatch',sha=main)
    def commit(self,message):
        tree=git(self.root,'write-tree').decode().strip()
        try:parents=['-p',git(self.root,'rev-parse','HEAD').decode().strip()]
        except subprocess.CalledProcessError:parents=[]
        head=git(self.root,'-c','user.name=Synthetic','-c','user.email=test@invalid.example','commit-tree',tree,*parents,'-m',message).decode().strip()
        git(self.root,'update-ref','HEAD',head);return head
    def run_driver(self,mode='daily',provider=None):
        with patch.object(d.daily.ingest,'utc_now',return_value=self.fixture.at.isoformat()):
            return d.Driver(self.root,self.remote,provider or self.fixture.transport,pin=self.remote.pin,writer='actions-v1',
                            context=self.context,clock=lambda:self.fixture.at,sleep=lambda _:None,
                            live_handover_probe=lambda _:copy.deepcopy(self.controller_observation)).execute(mode)
    def sync_main(self):
        main=self.remote.refs['heads/main'];git(self.root,'reset','--hard',main);self.context['sha']=main
    def test_daily_end_to_end_and_same_day_dedup(self):
        self.assertEqual(self.run_driver()['status'],'RESTORED');self.assertEqual(len(self.fixture.calls),9)
        self.sync_main();self.assertEqual(self.run_driver()['status'],'ALREADY_RESTORED')
        self.assertEqual(len(self.fixture.calls),9);self.assertEqual(self.remote.merge_writes,1)
    def test_wait_ci_resumes_without_acquisition(self):
        self.remote.ci_ready=False;self.assertEqual(self.run_driver()['status'],'WAIT_CI');count=len(self.fixture.calls)
        self.remote.ci_ready=True;self.assertEqual(self.run_driver()['status'],'RESTORED');self.assertEqual(len(self.fixture.calls),count)
    def test_lost_pr_ack_recovers_without_duplicate(self):
        self.remote.loss=lambda m,p:m=='POST' and p.endswith('/pulls')
        with self.assertRaises(r.RemoteStop):self.run_driver()
        self.assertEqual(len(self.remote.prs),1);count=len(self.fixture.calls)
        self.assertEqual(self.run_driver()['status'],'RESTORED');self.assertEqual(len(self.remote.prs),1);self.assertEqual(len(self.fixture.calls),count)
    def test_lost_merge_ack_recovers_without_remerge(self):
        self.remote.loss=lambda m,p:m=='PUT' and p.endswith('/merge')
        with self.assertRaises(r.RemoteStop):self.run_driver()
        self.sync_main();count=len(self.fixture.calls)
        self.assertEqual(self.run_driver()['status'],'RESTORED');self.assertEqual(self.remote.merge_writes,1);self.assertEqual(len(self.fixture.calls),count)
    def test_history_actual_batches_publish_restore(self):
        fixture=hf.HistoryTest();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.assertEqual(self.run_driver('history',fixture.transport)['status'],'RESTORED');self.assertEqual(len(fixture.calls),18)
        self.sync_main();self.assertEqual(self.run_driver('history',fixture.transport)['status'],'ALREADY_RESTORED');self.assertEqual(len(fixture.calls),18)
    def test_bad_context_has_no_writes(self):
        self.context['event']='pull_request'
        with self.assertRaises(r.RemoteStop):self.run_driver()
        self.assertFalse(self.fixture.calls);self.assertFalse(any(m!='GET' for m,_,_ in self.remote.calls))
    def test_negative_review_stops_even_with_null_decision(self):
        self.remote.reviews=[dict(id=1,state='CHANGES_REQUESTED',commit_id='synthetic')]
        with self.assertRaisesRegex(r.RemoteStop,'negative review'):self.run_driver()
        self.assertEqual(self.remote.merge_writes,0)

    def test_next_day_rejects_stale_handover_before_financial_repair(self):
        self.assertEqual(self.run_driver()['status'],'RESTORED');self.sync_main()
        self.fixture.at+=timedelta(days=1)
        with self.assertRaisesRegex(r.RemoteStop,'stale independent'):self.run_driver()
        # Existing runner tests cover the financial repair itself. A fixed receipt
        # cannot authorize a new day; this integration must stop before provider.
        self.assertEqual(len(self.fixture.calls),9)


    def test_github_budget_pause_is_durable_and_resumes(self):
        from scripts.actions_rate import BudgetedTransport
        from scripts.actions_sharded import ShardedStore
        import test_actions_daily_remote as authorization
        now=[10000.]
        def wait(seconds):now[0]+=seconds
        budget=BudgetedTransport(self.remote,clock=lambda:now[0],sleep=wait,deadline=now[0]+1200)
        budget.writes=budget.WRITE_LIMIT-63
        def run(client):return d.Driver(self.root,client,self.fixture.transport,pin=self.remote.pin,writer='actions-v1',
            context=self.context,clock=lambda:self.fixture.at,sleep=lambda _:None,
                            live_handover_probe=lambda _:copy.deepcopy(self.controller_observation)).execute('daily')
        self.assertEqual(run(budget)['status'],'PAUSED_GITHUB_BUDGET');self.assertFalse(self.fixture.calls)
        with tempfile.TemporaryDirectory() as tmp:
            document=ShardedStore(self.remote,authorization=authorization.authorization(),cache=tmp).read()[1]
        self.assertIn('github_pause',document);self.assertIn('github_budget',document)
        for _ in range(4):
            now[0]+=3600
            client=BudgetedTransport(self.remote,clock=lambda:now[0],sleep=wait,deadline=now[0]+20000)
            result=run(client)
            if result['status']=='RESTORED':break
        self.assertEqual(result['status'],'RESTORED');self.assertEqual(len(self.fixture.calls),9)

    def test_ci_wait_persists_read_budget_before_restart(self):
        from scripts.actions_rate import BudgetedTransport
        from scripts.actions_sharded import ShardedStore
        import test_actions_daily_remote as authorization
        now=[10000.]
        def wait(seconds):now[0]+=seconds
        self.remote.ci_ready=False
        client=BudgetedTransport(self.remote,clock=lambda:now[0],sleep=wait,deadline=now[0]+20000)
        result=d.Driver(self.root,client,self.fixture.transport,pin=self.remote.pin,writer='actions-v1',
            context=self.context,clock=lambda:self.fixture.at,sleep=lambda _:None,
                            live_handover_probe=lambda _:copy.deepcopy(self.controller_observation)).execute('daily')
        self.assertEqual(result['status'],'WAIT_CI')
        with tempfile.TemporaryDirectory() as tmp:
            document=ShardedStore(self.remote,authorization=authorization.authorization(),cache=tmp).read()[1]
        self.assertGreaterEqual(document['github_budget']['reads'],client.reads)
        self.assertGreaterEqual(document['github_budget']['writes'],client.writes)


class HistoryResumeTests(unittest.TestCase):
    def test_pause_after_last_known_response_advances_same_campaign_batch(self):
        fixture=DriverTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        provider=hf.HistoryTest();provider.setUp();self.addCleanup(provider.doCleanups)
        from scripts.actions_rate import BudgetPause
        original=d.history.ingest.publish_snapshot
        def pause(root,archive,day):
            if len(provider.calls)==12:raise BudgetPause('synthetic job deadline after known full batch')
            return original(root,archive,day)
        with patch.object(d.history.ingest,'publish_snapshot',side_effect=pause):
            self.assertEqual(fixture.run_driver('history',provider.transport)['status'],'PAUSED_GITHUB_BUDGET')
        self.assertEqual(len(provider.calls),12)
        self.assertEqual(fixture.run_driver('history',provider.transport)['status'],'RESTORED')
        self.assertEqual(len(provider.calls),18)
        self.assertEqual(fixture.remote.merge_writes,1)


class EarlyBudgetRestoreTests(unittest.TestCase):
    def test_restart_restores_budget_before_any_archive_download(self):
        fixture=DriverTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        from scripts.actions_rate import BudgetedTransport
        clock=[10000.]
        def wait(seconds):clock[0]+=seconds
        def execute(client):
            return d.Driver(fixture.root,client,fixture.fixture.transport,pin=fixture.remote.pin,writer='actions-v1',
                context=fixture.context,clock=lambda:fixture.fixture.at,sleep=lambda _:None,
                live_handover_probe=lambda _:dict(fixture.controller_observation)).execute('daily')
        fixture.remote.ci_ready=False
        client=BudgetedTransport(fixture.remote,clock=lambda:clock[0],sleep=wait)
        self.assertEqual(execute(client)['status'],'WAIT_CI')
        fresh=BudgetedTransport(fixture.remote,clock=lambda:clock[0],sleep=wait)
        original=pub.download_archive;observed=[]
        def download(*args,**kwargs):
            self.assertTrue(fresh.restored);self.assertGreater(fresh.reads,0);observed.append(True)
            return original(*args,**kwargs)
        with patch.object(pub,'download_archive',side_effect=download):
            self.assertEqual(execute(fresh)['status'],'WAIT_CI')
        self.assertTrue(observed);self.assertEqual(len(fixture.fixture.calls),9)
        invalid=BudgetedTransport(fixture.remote,clock=lambda:clock[0],sleep=wait)
        value=client.state();value['reads']=-1
        with self.assertRaisesRegex(r.RemoteStop,'invalid saved'):invalid.restore(value)
