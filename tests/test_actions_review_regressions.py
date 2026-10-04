from stock_jp.workspace import record_path
"""Independent review reproductions; all evidence and responses are synthetic."""
from tests.operation_test_support import prepare_write
import base64,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone,timedelta
import stock_jp.automation.publication as pub
import stock_jp.automation.daily_remote as r
import stock_jp.automation.rate as rate
import stock_jp.automation.http as http
import stock_jp.automation.attestation as attest
import stock_jp.automation.authorization as auth
from stock_jp.automation.git import git
import tests.test_actions_execution as f
import tests.test_actions_driver as integration

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
            prepare_write(hook).write_text('#!/bin/sh\ntouch '+str(marker)+'\n');hook.chmod(0o700)
            config=root/'global';prepare_write(config).write_text('[init]\n templateDir = '+str(template)+'\n[core]\n hooksPath = '+str(template/'hooks')+'\n')
            repo=root/'repo';repo.mkdir(parents=True)
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
        new.deadline=self.now+500
        with self.assertRaises(rate.BudgetPause):new.before_provider()
        self.assertEqual(self.calls,[])
        new.deadline=self.now+20000;new.before_provider()
        self.assertEqual(new.writes,0);self.assertEqual(self.calls,[])
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
        from tests.test_jquants_history import HistoryTest
        sources=[]
        for sessions in (8,4):
            h=HistoryTest();h.setUp();self.addCleanup(h.doCleanups)
            h.run_history(sessions=sessions)
            if sessions==8:h.run_history(sessions=sessions,batch=2)
            sources.append(record_path(h.private.parent, 'market-data'))
        store=fixture.root / 'data/market/jquants'
        pointer=r.codec.pack(sources[0],store);git(fixture.root,'add','data/market')
        approved=fixture.commit('Synthetic COMPLETE eight baseline')
        scope=fixture.scope;scope['baseline']={'main_sha':approved,'generation':pointer['generation']}
        r.codec.atomic(fixture.root/auth.MANIFEST,r.codec.encoded(scope))
        git(fixture.root,'add','.github');fixture.commit('Synthetic reviewed history baseline')
        r.codec.pack(sources[1],store);git(fixture.root,'add','data/market')
        weakened=fixture.commit('Synthetic weaker COMPLETE four')
        fixture.remote.refs['heads/main']=weakened
        with self.assertRaisesRegex(r.RemoteStop,'adopted history evidence changed'):
            auth.verify_scope(fixture.remote,approved_manifest_sha256=r.codec.digest(r.codec.encoded(scope)),
                mode='daily',writer='actions-v1',source=sources[1],now=fixture.fixture.at,
                handover_probe=lambda _:self.fail('must reject history before handover'))

class LiveHandoverTests(integration.DriverTests):
    def test_no_live_transport_cannot_activate_even_with_pinned_receipt(self):
        driver=integration.d.Driver(self.root,self.remote,self.fixture.transport,pin=self.remote.pin,writer='actions-v1',context=self.context)
        with self.assertRaisesRegex(r.RemoteStop,'live independent'):driver.execute('daily')
        self.assertEqual(self.remote.calls,[]);self.assertFalse(self.fixture.calls)
    def test_initialized_stale_observation_is_not_relabelled(self):
        self.assertEqual(self.run_driver()['status'],'RESTORED');self.sync_main()
        self.fixture.at+=timedelta(seconds=61)
        with self.assertRaisesRegex(r.RemoteStop,'stale independent'):self.run_driver()
        self.assertEqual(len(self.fixture.calls),9)
    def test_observation_time_is_preserved_exactly(self):
        original=self.controller_observation['observed_at']
        observed=attest.verify(self.remote,self.scope,self.remote.pin,main=self.context['sha'],initialized=True,
            now=self.fixture.at+timedelta(seconds=20),live_probe=lambda _:dict(self.controller_observation))
        self.assertEqual(observed['observed_at'],original)
    def test_revived_lease_stops_initialized_runner(self):
        self.assertEqual(self.run_driver()['status'],'RESTORED');self.sync_main()
        self.controller_observation.update(lease_state='active',lease_owner='synthetic-old-writer',active_run_ids=['revived'])
        with self.assertRaisesRegex(r.RemoteStop,'lease is active'):self.run_driver()
        self.assertEqual(len(self.fixture.calls),9)
    def test_old_checkout_is_rejected_before_writes(self):
        self.context['sha']=self.base
        before=len(self.remote.calls)
        with self.assertRaisesRegex(r.RemoteStop,'untrusted workflow'):self.run_driver()
        self.assertFalse(self.fixture.calls);self.assertTrue(all(m=='GET' for m,_,_ in self.remote.calls[before:]))
    def test_wrong_handover_identity_rejected(self):
        self.controller_observation['lease_id']='other-lease'
        with self.assertRaisesRegex(r.RemoteStop,'identity mismatch'):self.run_driver()
        self.assertFalse(self.fixture.calls)

for name in dir(integration.DriverTests):
    if name.startswith('test_'):setattr(LiveHandoverTests,name,None)

class FullTreeScopeTests(unittest.TestCase):
    def test_real_complete_280_history_over_300_files_is_authorized(self):
        fixture=integration.DriverTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        from tests.test_jquants_history import HistoryTest
        history=HistoryTest();history.setUp();self.addCleanup(history.doCleanups)
        for batch in range(1,48):report=history.run_history(sessions=280,batch=batch)
        self.assertEqual(report['status'],'COMPLETE');self.assertEqual(report['requests_used'],562)
        source=record_path(history.private.parent, 'market-data');store=fixture.root / 'data/market/jquants'
        fixture.scope['expires_at']=(fixture.now+timedelta(days=7)).isoformat()
        r.codec.atomic(fixture.root/auth.MANIFEST,r.codec.encoded(fixture.scope))
        fixture.remote.pin=r.codec.digest(r.codec.encoded(fixture.scope))
        r.codec.pack(source,store);git(fixture.root,'add','data/market','.github')
        head=fixture.commit('Synthetic adopted COMPLETE 280 history')
        fixture.remote.refs['heads/main']=head
        before=auth.complete_tree(fixture.remote,fixture.base);after=auth.complete_tree(fixture.remote,head)
        self.assertGreater(sum(before.get(k)!=v for k,v in after.items()),300)
        kwargs=dict(approved_manifest_sha256=fixture.remote.pin,mode='daily',writer='actions-v1',source=source,
            now=fixture.fixture.at,handover_probe=lambda _:attest.verify(fixture.remote,fixture.scope,fixture.remote.pin,
                main=head,initialized=True,now=fixture.fixture.at,live_probe=lambda _:dict(fixture.controller_observation)))
        _,facts=auth.verify_scope(fixture.remote,**kwargs);self.assertTrue(facts['main_restored_and_verified'])
        # Same full tree traversal must reject hidden code changes beyond API page 300.
        original=fixture.remote.handle
        def changed(method,path,body):
            result=original(method,path,body)
            if '/git/trees/' in path and method=='GET':
                commit_tree=git(fixture.root,'rev-parse',head+'^{tree}').decode().strip()
                if commit_tree in path:result['tree'].append(dict(path='scripts/unapproved.py',mode='100644',type='blob',sha='f'*40))
            return result
        fixture.remote.handle=changed
        with self.assertRaisesRegex(r.RemoteStop,'beyond approved'):auth.verify_scope(fixture.remote,**kwargs)
        fixture.remote.handle=original;fixture.sync_main();fixture.remote.store_bodies=False
        fixture.fixture.at+=timedelta(days=3)  # Monday after the adopted Friday history
        clock=[fixture.fixture.at.timestamp()];started=clock[0];calls=len(fixture.remote.calls)
        def now():return datetime.fromtimestamp(clock[0],fixture.fixture.at.tzinfo)
        def wait(seconds):clock[0]+=seconds
        budget=rate.BudgetedTransport(fixture.remote,clock=lambda:clock[0],sleep=wait)
        with patch.object(integration.d.daily.ingest,'utc_now',side_effect=lambda:now().isoformat()):
            result=integration.d.Driver(fixture.root,budget,fixture.fixture.transport,pin=fixture.remote.pin,
                writer='actions-v1',context=fixture.context,clock=now,sleep=lambda _:None,
                live_handover_probe=lambda _:{**fixture.controller_observation,'observed_at':now().isoformat()}).execute('daily')
        self.assertEqual(result['status'],'RESTORED');self.assertEqual(len(fixture.fixture.calls),9)
        self.assertLess(clock[0]-started,5*3600)
        print(json.dumps(dict(cold_business_daily_280=True,provider_calls=len(fixture.fixture.calls),
            github_rest_calls=len(fixture.remote.calls)-calls,simulated_elapsed_seconds=round(clock[0]-started,1))),flush=True)
    def test_truncated_tree_never_accepted(self):
        fixture=integration.DriverTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        original=fixture.remote.handle
        def truncated(method,path,body):
            result=original(method,path,body)
            if '/git/trees/' in path:result['truncated']=True
            return result
        fixture.remote.handle=truncated
        with self.assertRaisesRegex(r.RemoteStop,'truncated'):auth.complete_tree(fixture.remote,fixture.base)

class ReviewFoldingTests(unittest.TestCase):
    def row(self,ident,owner,state,head='a'*40):
        return dict(id=ident,user={'id':owner,'login':'synthetic'},state=state,commit_id=head,
                    submitted_at='2026-10-03T00:00:00Z')
    def test_later_same_head_approval_clears_only_its_reviewers_negative(self):
        rows=[self.row(1,11,'CHANGES_REQUESTED'),self.row(2,11,'APPROVED')]
        http.reject_negative_reviews(list(reversed(rows)),'a'*40)
        rows.append(self.row(3,22,'CHANGES_REQUESTED'))
        with self.assertRaisesRegex(r.RemoteStop,'negative review'):http.reject_negative_reviews(rows,'a'*40)
    def test_comment_or_other_head_approval_does_not_clear_negative(self):
        negative=self.row(1,11,'CHANGES_REQUESTED')
        for later in [self.row(2,11,'COMMENTED'),self.row(2,11,'APPROVED','b'*40)]:
            with self.assertRaisesRegex(r.RemoteStop,'negative review'):
                http.reject_negative_reviews([negative,later],'a'*40)
    def test_later_negative_and_explicit_dismissal(self):
        with self.assertRaisesRegex(r.RemoteStop,'negative review'):
            http.reject_negative_reviews([self.row(1,11,'APPROVED'),self.row(2,11,'CHANGES_REQUESTED')],'a'*40)
        http.reject_negative_reviews([self.row(1,11,'CHANGES_REQUESTED'),self.row(2,11,'DISMISSED')],'a'*40)
