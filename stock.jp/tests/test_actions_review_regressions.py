"""Independent review reproductions; all evidence and responses are synthetic."""
import base64,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone,timedelta
from scripts import actions_publication as pub, actions_daily_remote as r, actions_rate as rate
from scripts import actions_http as http, actions_attestation as attest, actions_authorization as auth
from scripts.git_sandbox import git
import test_actions_execution as f
import test_actions_driver as integration

class ReviewTests(unittest.TestCase):
    def test_pending_then_success_across_pages_latest_context(self):
        stamp='2026-10-03T00:00:00Z'
        newer=[dict(id=i+100,context='context-'+str(i),state='success',created_at=stamp) for i in range(100)]
        older=[dict(id=1,context='context-0',state='pending',created_at=stamp)]
        rows=pub.pages(lambda m,p,b:newer if p.endswith('page=1') else older,'/commits/'+'a'*40+'/statuses')
        folded=pub.latest_statuses(rows,'a'*40)
        self.assertEqual(len(folded),100);self.assertTrue(all(x['state']=='success' for x in folded))
        for invalid in ('unknown',None):
            with self.assertRaises(r.RemoteStop):pub.latest_statuses([{**older[0],'state':invalid}],'a'*40)
    def test_negative_review_never_uses_nullable_policy(self):
        opener=f.Opener();client=http.GitHubTransport('synthetic',opener=opener)
        with self.assertRaisesRegex(r.RemoteStop,'negative review'):
            client.review_decision(1,'a'*40,[dict(state='CHANGES_REQUESTED',commit_id='a'*40)])
        self.assertEqual(opener.calls,[])
    def test_git_global_template_and_environment_injection_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);template=root/'templates';(template/'hooks').mkdir(parents=True)
            marker=root/'EXECUTED';hook=template/'hooks/post-checkout'
            hook.write_text('#!/bin/sh\ntouch '+str(marker)+'\n');hook.chmod(0o700)
            config=root/'global';config.write_text('[init]\n templateDir = '+str(template)+'\n[core]\n hooksPath = '+str(template/'hooks')+'\n')
            repo=root/'repo';repo.mkdir()
            with patch.dict(os.environ,{'GIT_CONFIG_GLOBAL':str(config),'GIT_TEMPLATE_DIR':str(template),
                'GIT_DIR':str(root/'wrong'),'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'core.hooksPath','GIT_CONFIG_VALUE_0':str(template/'hooks')}):
                git(repo,'init','--quiet');git(repo,'-c','user.name=S','-c','user.email=s@invalid.example','commit','--allow-empty','-m','fixture')
                git(repo,'checkout','--detach','HEAD')
            self.assertFalse(marker.exists());self.assertFalse((repo/'.git/hooks/post-checkout').exists())

class RateTests(unittest.TestCase):
    def make(self):
        self.now=10000.;self.calls=[]
        def wait(seconds):self.now+=seconds
        def request(m,p,b):self.calls.append((self.now,m));return {}
        return rate.BudgetedTransport(request,clock=lambda:self.now,sleep=wait,deadline=self.now+20000)
    def test_pacing_independent_counts_and_restart_carry(self):
        client=self.make()
        for _ in range(20):client('POST','/synthetic',{})
        self.assertTrue(all(b[0]-a[0]>=10.09 for a,b in zip(self.calls,self.calls[1:])))
        saved=client.state();new=self.make();new.restore(saved);new.restore(saved)
        self.assertEqual(new.writes,saved['writes'])
        new.writes=new.WRITE_LIMIT-63
        with self.assertRaises(rate.BudgetPause):new.before_provider()
        self.assertEqual(self.calls,[])
    def test_retry_after_headers_cover_rest_and_graphql(self):
        from urllib.error import HTTPError
        error=HTTPError('unused',429,'synthetic',{'retry-after':'120'},None)
        client=http.GitHubTransport('synthetic',opener=f.Opener(error=error))
        with patch.object(http.time,'time',return_value=1000):
            for operation in (lambda:client('GET',pub.PREFIX),lambda:client.review_decision(1,'a'*40,[])):
                with self.assertRaises(rate.RateLimited) as caught:operation()
                self.assertEqual(caught.exception.until,1120)

    def test_known_rate_limit_waits_unknown_not_retried(self):
        client=self.make();attempts=[]
        def limited(*args):
            attempts.append(self.now)
            if len(attempts)==1:raise rate.RateLimited(self.now+120)
            return {}
        client.request=limited;client('GET','/synthetic');self.assertGreaterEqual(attempts[1]-attempts[0],120)
        attempts.clear()
        def unknown(*args):attempts.append(1);raise OSError('unknown')
        client.request=unknown
        with self.assertRaises(OSError):client('POST','/synthetic',{})
        self.assertEqual(attempts,[1])

class RecoveryTests(integration.DriverTests):
    # Do not duplicate inherited E2E methods in discovery below.
    def test_wrong_old_pr_cannot_satisfy_lost_ack(self):
        self.remote.loss=lambda m,p:m=='POST' and p.endswith('/pulls')
        with self.assertRaises(r.RemoteStop):self.run_driver()
        self.remote.prs[1]['state']='closed';self.remote.prs[1]['head']['sha']=self.base
        with self.assertRaisesRegex(r.RemoteStop,'not uniquely confirmed'):self.run_driver()
        self.assertEqual(self.remote.merge_writes,0);self.assertEqual(len(self.fixture.calls),9)
    def test_controller_evidence_required_before_first_write(self):
        original=self.remote.handle
        def no_evidence(m,p,b):
            if '/issues/900/comments' in p:return []
            return original(m,p,b)
        self.remote.handle=no_evidence
        with self.assertRaisesRegex(r.RemoteStop,'independent owner'):self.run_driver()
        self.assertFalse(self.fixture.calls);self.assertFalse(any(m!='GET' for m,_,_ in self.remote.calls))
    def test_controller_reenabled_receipt_rejected(self):
        original=self.remote.handle
        def changed(m,p,b):
            result=original(m,p,b)
            if '/contents/'+attest.HANDOVER in p:
                receipt=json.loads(base64.b64decode(result['content']));receipt['enabled']=True
                result['content']=base64.b64encode(json.dumps(receipt).encode()).decode()
            return result
        self.remote.handle=changed
        with self.assertRaisesRegex(r.RemoteStop,'handover receipt'):self.run_driver()
        self.assertFalse(self.fixture.calls);self.assertFalse(any(m!='GET' for m,_,_ in self.remote.calls))

for name in dir(integration.DriverTests):
    if name.startswith('test_'):setattr(RecoveryTests,name,None)

class ApprovedHistoryTests(unittest.TestCase):
    def test_complete_eight_cannot_be_replaced_by_complete_four(self):
        fixture=integration.DriverTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        from test_jquants_history import HistoryTest
        sources=[]
        for sessions in (8,4):
            h=HistoryTest();h.setUp();self.addCleanup(h.doCleanups)
            h.run_history(sessions=sessions)
            if sessions==8:h.run_history(sessions=sessions,batch=2)
            sources.append(h.private/'market-data')
        store=fixture.root/'stock.jp/data/jquants-archive'
        pointer=r.codec.pack(sources[0],store);git(fixture.root,'add','stock.jp/data')
        approved=fixture.commit('Synthetic COMPLETE eight baseline')
        scope=fixture.scope;scope['baseline']={'main_sha':approved,'generation':pointer['generation']}
        r.codec.atomic(fixture.root/auth.MANIFEST,r.codec.encoded(scope))
        git(fixture.root,'add','.github');fixture.commit('Synthetic reviewed history baseline')
        r.codec.pack(sources[1],store);git(fixture.root,'add','stock.jp/data')
        weakened=fixture.commit('Synthetic weaker COMPLETE four')
        fixture.remote.refs['heads/main']=weakened
        with self.assertRaisesRegex(r.RemoteStop,'adopted history evidence changed'):
            auth.verify_scope(fixture.remote,approved_manifest_sha256=r.codec.digest(r.codec.encoded(scope)),
                mode='daily',writer='actions-v1',source=sources[1],now=fixture.fixture.at,
                handover_probe=lambda _:self.fail('must reject history before handover'))
