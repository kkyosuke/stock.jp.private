import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from scripts import actions_http as http, actions_daily_remote as r, actions_publication as pub
from scripts import actions_authorization as auth, jquants_ingest as ingest
import test_actions_daily_remote as fixtures
import test_jquants_history as hf

class Response(io.BytesIO):
    def __init__(self,data,url,status=200):super().__init__(data);self.url=url;self.status=status
    def geturl(self):return self.url

class Opener:
    def __init__(self,response=None,error=None):self.response=response;self.error=error;self.calls=[]
    def open(self,request,timeout):
        self.calls.append(request)
        if self.error:raise self.error
        return self.response or Response(b'{}',request.full_url)

class TransportTests(unittest.TestCase):
    def test_origin_and_redirect(self):
        opener=Opener();transport=http.GitHubTransport('synthetic-secret',opener=opener)
        for path in ('https://evil.invalid/','//evil.invalid/x','/repos/other/private',
                     '/repos/'+r.REPO+'/../secrets','/repos/'+r.REPO+'/%2e%2e/secrets'):
            with self.assertRaises(r.RemoteStop):transport('GET',path)
        self.assertFalse(opener.calls)
        opener.response=Response(b'{}','https://evil.invalid/')
        with self.assertRaises(r.RemoteStop):transport('GET','/repos/'+r.REPO)
        self.assertIsNone(http.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.invalid'))
    def test_denied_and_unknown_writes_do_not_leak_or_retry(self):
        for error in (HTTPError('unused',403,'synthetic-secret',{},io.BytesIO(b'synthetic-secret')),
                      TimeoutError('synthetic-secret')):
            opener=Opener(error=error)
            with self.assertRaises(r.RemoteStop) as caught:
                http.GitHubTransport('synthetic-secret',opener=opener)('POST','/repos/'+r.REPO+'/git/blobs',{})
            self.assertNotIn('synthetic-secret',str(caught.exception));self.assertEqual(len(opener.calls),1)
    def test_oversized_duplicate_json(self):
        for raw in (b'x'*20,b'{"x":1,"x":2}'):
            client=http.FixedHTTP('https://api.github.com',{},opener=Opener(Response(raw,'https://api.github.com/x')),limit=15)
            with self.assertRaises(r.RemoteStop):client.json('GET','/x')
    def test_archive_limit_fits_base64_http_upload_and_download(self):
        import base64
        from scripts import market_data_git as codec
        raw = b'x' * codec.MAX_SHARD
        encoded = base64.b64encode(raw).decode()
        # Include escaped CRLF wrapping and a conservative REST metadata envelope.
        download = dict(content='\r\n'.join(encoded[i:i+60] for i in range(0,len(encoded),60)), encoding='base64',
                        size=len(raw), sha='a'*40, url='x'*4096)
        upload = dict(content=encoded, encoding='base64')
        self.assertLess(len(json.dumps(download).encode()), 32*1024*1024)
        opener = Opener(Response(json.dumps(download).encode(), 'https://api.github.com/x'))
        client = http.FixedHTTP('https://api.github.com', {}, opener=opener)
        self.assertEqual(client.json('POST', '/x', upload)[0], 200)
        with tempfile.TemporaryDirectory() as tmp:
            codec.put_plain(Path(tmp), raw, '.bin')
            with self.assertRaises(codec.ArchiveError):
                codec.put_plain(Path(tmp), raw+b'x', '.bin')

    def test_provider_errors(self):
        opener=Opener(error=HTTPError('unused',429,'secret',{},io.BytesIO()))
        with self.assertRaises(ingest.ApiError) as caught:http.ProviderTransport('synthetic',opener=opener)('/markets/calendar',{})
        self.assertEqual(caught.exception.status,429)
        with self.assertRaises(r.RemoteStop):http.ProviderTransport('synthetic',opener=opener)('/unreviewed',{})
    def test_force_and_unbound_merge_never_reach_http(self):
        opener=Opener();transport=http.GitHubTransport('synthetic',opener=opener)
        for method,path,body in [('PATCH','/git/refs/heads/automation/daily-checkpoint-v1',{'force':True}),
                                 ('PUT','/pulls/1/merge',{'merge_method':'squash'})]:
            with self.assertRaises(r.RemoteStop):transport(method,'/repos/'+r.REPO+path,body)
        self.assertFalse(opener.calls)

class HistoryRemoteTests(unittest.TestCase):
    transport=hf.HistoryTest.transport
    run_history=hf.HistoryTest.run_history
    def setUp(self):
        hf.HistoryTest.setUp(self);self.git=fixtures.GitMock()
        self.store=r.GitCheckpointStore(self.git,authorization=fixtures.authorization())
        self.campaign=dict(as_of=str(self.end),sessions=8,
            config_sha256=r.codec.digest((self.private/'source-config.json').read_bytes()),batch_limit=12)
    def remote(self):return r.HistoryCheckpoint(self.store,writer='actions-v1',authorization=fixtures.authorization(),campaign=self.campaign)
    def go(self,**kwargs):return self.run_history(remote=self.remote(),**kwargs)
    def fresh(self):shutil.rmtree(self.private/'market-data')
    def test_batches_resume_and_same_run_no_repeat(self):
        self.assertEqual(self.go()['requests_used'],12);self.fresh()
        self.assertEqual(self.go()['requests_used'],12);self.assertEqual(len(self.calls),12);self.fresh()
        result=self.go(batch=2);self.assertEqual(result['status'],'COMPLETE');self.assertEqual(result['requests_used'],18)
        self.assertEqual(self.store.read()[1]['history']['phase'],'COLLECTED')
        self.fresh();self.assertEqual(self.go(batch=2),result);self.assertEqual(len(self.calls),18)
    def test_crash_cannot_advance_or_retry(self):
        def crash(*_):raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.go(transport=crash)
        self.fresh()
        with self.assertRaisesRegex(r.RemoteStop,'ambiguous'):self.go(batch=2)
        self.assertEqual(len(r.history_receipts(self.store.read()[1]['history']['state'])),1)
    def test_existing_local_campaign_not_imported(self):
        self.run_history();count=len(self.calls)
        with self.assertRaisesRegex(r.RemoteStop,'import forbidden'):self.go()
        self.assertEqual(len(self.calls),count);self.assertFalse(any(m!='GET' for m,_,_ in self.git.calls))
    def test_known_failure_reuses_budget(self):
        def fail(*_):raise ingest.ApiError(503)
        self.assertEqual(self.go(transport=fail)['requests_used'],1);self.fresh()
        self.assertEqual(self.go()['requests_used'],12);self.assertEqual(len(self.calls),11)
    def test_rejected_scope(self):
        adapter=self.remote();adapter.authorization['publication_rejected']=True
        with self.assertRaises(r.RemoteStop):self.run_history(remote=adapter)
        self.assertFalse(self.git.calls);self.assertFalse(self.calls)
    def test_adopted_history_preserved_and_marker_deletion_rejected(self):
        self.run_history();self.run_history(batch=2);source=self.private/'market-data'
        files={p.relative_to(source).as_posix():r.codec.digest(p.read_bytes()) for p in source.rglob('*')
               if p.is_file() and p.name!='.ingestion.lock' and
               ('history' in str(p) or '/objects/' in str(p) or '/revisions/' in str(p))}
        boundary=auth.AdoptedHistory(source,files);snapshot=r.capture(source,boundary.check)
        with tempfile.TemporaryDirectory() as tmp:r.unpack(snapshot,Path(tmp)/'restored',boundary.check)
        (source/'jquants/history-marker.json').unlink()
        with self.assertRaises(r.RemoteStop):boundary.check(source)

class EvidenceTests(unittest.TestCase):
    def test_pagination(self):
        calls=[]
        def request(method,path,body):
            calls.append(path)
            return [{'id':n} for n in range(100)] if path.endswith('&page=1') else [{'id':100}]
        self.assertEqual(len(pub.pages(request,'/pulls')),101);self.assertEqual(len(calls),2)
        with self.assertRaises(r.RemoteStop):pub.pages(lambda *a:[{'id':1}]*100,'/pulls')
    def test_publication_disabled(self):
        calls=[]
        with self.assertRaises(r.RemoteStop):pub.publish_prevalidated(lambda *a:calls.append(a),proof={},tree_sha='a'*40,entries=[],blobs={})
        self.assertFalse(calls)
    def test_mismatched_generation(self):
        with self.assertRaises(r.RemoteStop):pub.mark_adopted(None,verified_main='a'*40,verified_generation='b'*64,actual_generation='c'*64)

class ApprovalTests(unittest.TestCase):
    def test_missing_pin_and_rejected_approval_stop(self):
        import base64
        from datetime import datetime,timezone,timedelta
        calls=[]
        scope=dict(version=1,enabled=True,repository=r.REPO,writer='actions-v1',modes=['daily'],
                   publication_rejections=['synthetic-rejected'],unresolved_campaigns=[],mode='PAPER',
                   submission='HUMAN_ONLY',plan='light',addons=[],daily_limit=12,history_limits=[280,600,50,12],
                   price_coverage_min=.98,active_coverage_min=1.,expires_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat())
        raw=r.codec.encoded(scope)
        def request(method,path,body):
            calls.append(path)
            if path.endswith('/'+r.REPO):return {'private':True,'full_name':r.REPO}
            if path.endswith('/git/ref/heads/main'):return {'object':{'sha':'a'*40}}
            return {'type':'file','encoding':'base64','content':base64.b64encode(raw).decode()}
        kwargs=dict(mode='daily',writer='actions-v1',source=Path('/unused'),handover_probe=lambda _:self.fail('handover must not run'))
        with self.assertRaises(r.RemoteStop):auth.verify_scope(request,approved_manifest_sha256=None,**kwargs)
        self.assertFalse(calls)
        with self.assertRaisesRegex(r.RemoteStop,'rejection'):auth.verify_scope(request,approved_manifest_sha256=r.codec.digest(raw),**kwargs)
        with self.assertRaisesRegex(r.RemoteStop,'reviewed pin'):auth.verify_scope(request,approved_manifest_sha256='b'*64,**kwargs)

class MergeTests(unittest.TestCase):
    def setUp(self):
        import base64
        from datetime import datetime,timezone
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        source=Path(self.tmp.name)/'source';source.mkdir()
        self.store=Path(self.tmp.name)/'store';self.pointer=r.codec.pack(source,self.store)
        self.base,self.head,self.merged='a'*40,'b'*40,'c'*40
        self.did_merge=False;self.calls=[];self.bad_status=False
        self.proof=dict(validated=True,repository=r.REPO,kind='market',run_id='2026-10-03',base_sha=self.base,
                        head_sha=self.head,paths=['stock.jp/data/jquants-archive/current.json'],
                        validated_at=datetime.now(timezone.utc).isoformat())
        self.blobs={r.git_blob(p.read_bytes()):p.read_bytes() for p in self.store.rglob('*') if p.is_file()}
        self.members=[dict(path='stock.jp/data/jquants-archive/'+p.relative_to(self.store).as_posix(),
                           mode='100644',type='blob',sha=r.git_blob(p.read_bytes())) for p in self.store.rglob('*') if p.is_file()]
    def request(self,method,path,body):
        import base64
        self.calls.append((method,path,body));suffix=path.removeprefix('/repos/'+r.REPO)
        if suffix=='':return dict(full_name=r.REPO,private=True,allow_auto_merge=True)
        if suffix=='/git/ref/heads/main':return {'object':{'sha':self.merged if self.did_merge else self.base}}
        if suffix=='/pulls/1':return dict(state='open',draft=False,merged=False,mergeable=True,user={'login':'kkyosuke'},
            head=dict(sha=self.head,ref='daily/market/2026-10-03',repo={'full_name':r.REPO}),base=dict(sha=self.base,ref='main'))
        if '/check-runs?' in suffix:return {'total_count':3,'check_runs':[dict(id=i,name=n,head_sha=self.head,status='completed',conclusion='success',app={'slug':'github-actions'}) for i,n in enumerate(('format','test','guardrail'))]}
        if '/statuses?' in suffix:return [dict(id=1,context='synthetic-ci',created_at='2026-10-03T00:00:00Z',state='failure' if self.bad_status else 'success')]
        if suffix.startswith('/actions/runs?'):return {'total_count':3,'workflow_runs':[dict(id=i,head_sha=self.head,status='completed',conclusion='success',event='pull_request',path=p) for i,p in enumerate(pub.publication.WORKFLOWS['market'])]}
        if '/reviews?' in suffix:return []
        if '/files?' in suffix:return [dict(filename=self.proof['paths'][0])]
        if suffix=='/pulls/1/merge':
            self.assertEqual(body,{'sha':self.head,'merge_method':'squash'});self.did_merge=True
            return dict(merged=True,sha=self.merged)
        if suffix.startswith('/git/commits/'):return dict(sha=self.merged,tree={'sha':'d'*40})
        if suffix.startswith('/git/trees/'):return dict(truncated=False,tree=self.members)
        if suffix.startswith('/git/blobs/'):
            raw=self.blobs[suffix.rsplit('/',1)[1]]
            return dict(encoding='base64',content=base64.b64encode(raw).decode(),size=len(raw))
        raise AssertionError(suffix)
    def test_normal_sha_guarded_merge_and_remote_restore(self):
        result=pub.merge_and_restore(self.request,self.proof,1,review_probe=lambda *a:None,
            expected_generation=self.pointer['generation'],authorization=fixtures.authorization())
        self.assertEqual(result['status'],'RESTORED')
    def test_failed_status_or_review_never_merges(self):
        self.bad_status=True
        with self.assertRaises(ValueError):pub.merge_and_restore(self.request,self.proof,1,review_probe=lambda *a:None,
            expected_generation=self.pointer['generation'],authorization=fixtures.authorization())
        self.assertFalse(self.did_merge)
        self.bad_status=False
        with self.assertRaises(r.RemoteStop):pub.merge_and_restore(self.request,self.proof,1,review_probe=lambda *a:'REVIEW_REQUIRED',
            expected_generation=self.pointer['generation'],authorization=fixtures.authorization())
        self.assertFalse(self.did_merge)
    def test_merge_ack_loss_not_retried(self):
        def lost(method,path,body):
            result=self.request(method,path,body)
            if method=='PUT':raise r.RemoteStop('unknown result')
            return result
        with self.assertRaises(r.RemoteStop):pub.merge_and_restore(lost,self.proof,1,review_probe=lambda *a:None,
            expected_generation=self.pointer['generation'],authorization=fixtures.authorization())
        self.assertEqual(sum(m=='PUT' for m,_,_ in self.calls),1)

class ScopeIntegrationTests(MergeTests):
    def test_scope_reads_main_archive_and_independent_handover(self):
        import base64
        from datetime import datetime,timezone,timedelta
        self.did_merge=True;now=datetime.now(timezone.utc)
        source=Path(self.tmp.name)/'scope-source';source.mkdir()
        scope=dict(version=1,enabled=True,repository=r.REPO,writer='actions-v1',modes=['daily'],
            publication_rejections=[],unresolved_campaigns=[],mode='PAPER',submission='HUMAN_ONLY',plan='light',addons=[],
            daily_limit=12,history_limits=[280,600,50,12],price_coverage_min=.98,active_coverage_min=1.,
            expires_at=(now+timedelta(days=1)).isoformat(),data_publication_authorized=True,checkpoint_authorized=True,
            old_scheduler_id='synthetic-old',baseline={'main_sha':self.base,'generation':self.pointer['generation']})
        raw=r.codec.encoded(scope)
        def request(method,path,body):
            if '/contents/'+auth.MANIFEST in path:return dict(type='file',encoding='base64',content=base64.b64encode(raw).decode())
            if '/compare/' in path:return dict(status='ahead',total_commits=1,files=[{'filename':auth.MANIFEST,'status':'added'}])
            if '/contents/stock.jp/data/' in path:return dict(content=base64.b64encode((self.store/'current.json').read_bytes()).decode())
            if '/pulls?state=open' in path:return []
            if '/git/commits/' in path:return dict(sha=path.rsplit('/',1)[1],tree={'sha':'d'*40})
            return self.request(method,path,body)
        observed=dict(scheduler_id='synthetic-old',enabled=False,active_writers=0,next_writer='actions-v1',observed_at=now.isoformat())
        kwargs=dict(approved_manifest_sha256=r.codec.digest(raw),mode='daily',writer='actions-v1',source=source,now=now)
        _,facts=auth.verify_scope(request,handover_probe=lambda _:observed,**kwargs)
        self.assertTrue(facts['old_scheduler_stopped'])
        with self.assertRaisesRegex(r.RemoteStop,'handed over'):
            auth.verify_scope(request,handover_probe=lambda _:{**observed,'active_writers':1},**kwargs)
        with self.assertRaisesRegex(r.RemoteStop,'stale'):
            auth.verify_scope(request,handover_probe=lambda _:{**observed,'observed_at':(now-timedelta(minutes=2)).isoformat()},**kwargs)

class BootstrapTests(unittest.TestCase):
    def test_create_only_checkpoint_preserves_same_day_budget(self):
        fixture=fixtures.RemoteDailyTests('test_normal_and_fresh_runner_same_day_dedup')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.run_daily()
        git=fixture.git;git.head=None
        original=git.__class__.__call__
        from unittest.mock import patch
        def create(mock,method,path,body):
            if method=='POST' and path.endswith('/git/refs'):
                if mock.head is not None:raise OSError('existing ref')
                mock.head=body['sha'];return {'object':{'sha':mock.head}}
            return original(mock,method,path,body)
        with patch.object(git.__class__,'__call__',create):
            pub.bootstrap_checkpoint(fixture.store,fixture.private/'market-data','actions-v1')
            run=fixture.store.read()[1]['runs']['2026-10-02']
            self.assertEqual(len(run['state']['requests']),9);self.assertEqual(run['phase'],'RESTORED')
            fixture.fresh_disk()
            self.assertEqual(fixture.remote_run()['requests_used'],9)
            self.assertEqual(len(fixture.calls),9)
            with self.assertRaises(r.RemoteStop):pub.bootstrap_checkpoint(fixture.store,fixture.private/'market-data','actions-v1')

class CandidateTests(unittest.TestCase):
    def test_noop_and_real_synthetic_candidate_publication(self):
        import base64
        from datetime import timedelta
        import subprocess
        fixture=fixtures.RemoteDailyTests('test_normal_and_fresh_runner_same_day_dedup')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.run_daily();source=fixture.private/'market-data'
        r.codec.pack(source,fixture.root/'stock.jp/data/jquants-archive')
        def git(*args):return subprocess.check_output(['git','-C',str(fixture.root),*args],stderr=subprocess.DEVNULL).decode().strip()
        git('init','--quiet');git('add','stock.jp/data/jquants-archive')
        git('-c','user.name=Synthetic','-c','user.email=synthetic@invalid.example','commit','--quiet','-m','Synthetic base')
        base=git('rev-parse','HEAD')
        with pub.prepare_candidate(fixture.root,source,'2026-10-02',base) as candidate:
            self.assertEqual(candidate['status'],'NOOP')
        fixture.at+=timedelta(days=1);fixture.run_daily()
        with pub.prepare_candidate(fixture.root,source,'2026-10-03',base) as candidate:
            self.assertEqual(candidate['status'],'CANDIDATE')
            calls=[];head='f'*40
            def request(method,path,body):
                calls.append((method,path,body))
                if path.endswith('/git/ref/heads/main'):return {'object':{'sha':base}}
                if '/pulls?state=open' in path:return []
                if path.endswith('/git/commits/'+base):return {'tree':{'sha':git('rev-parse',base+'^{tree}')}}
                if path.endswith('/git/blobs'):return {'sha':r.git_blob(base64.b64decode(body['content']))}
                if path.endswith('/git/trees'):return {'sha':candidate['tree_sha']}
                if path.endswith('/git/commits'):return {'sha':head}
                if path.endswith('/git/commits/'+head):return {'tree':{'sha':candidate['tree_sha']},'parents':[{'sha':base}]}
                if path.endswith('/git/refs'):return {'object':{'sha':head}}
                if path.endswith('/pulls'):return {'number':123}
                raise AssertionError(path)
            args={k:candidate[k] for k in ('root','proof','tree_sha','entries','blobs')}
            result=pub.publish_prevalidated(request,authorization=fixtures.authorization(),**args)
            self.assertEqual(result['status'],'WAIT_CI');self.assertEqual(result['proof']['head_sha'],head)
            self.assertTrue(all('force' not in (body or {}) for _,_,body in calls))

class IntentTests(unittest.TestCase):
    def setUp(self):
        import copy
        from scripts.actions_intent import IntentTransport
        class Checkpoint:
            authorization=fixtures.authorization()
            def __init__(self):self.document={};self.saved=[]
            def commit(self):self.saved.append(copy.deepcopy(self.document))
        self.checkpoint=Checkpoint();self.writes=[]
        def request(method,path,body):
            if method!='GET':
                self.assertEqual(self.checkpoint.saved[-1]['publication_intent']['state'],'PENDING')
                self.writes.append((method,path,body));raise r.RemoteStop('ACK lost')
            return {'object':{'sha':'a'*40}}
        self.transport=IntentTransport(self.checkpoint,request)
    def test_ref_ack_loss_recovered_by_read_without_replay(self):
        path='/repos/'+r.REPO+'/git/refs'
        with self.assertRaises(r.RemoteStop):self.transport('POST',path,{'ref':'refs/heads/daily/market/2026-10-03','sha':'a'*40})
        with self.assertRaises(r.RemoteStop):self.transport('POST',path,{})
        self.assertEqual(self.transport.recover_readonly(),{'object':{'sha':'a'*40}})
        self.assertEqual(len(self.writes),1)
        self.assertEqual(self.checkpoint.saved[-1]['publication_intent']['state'],'ACKED')
    def test_opaque_commit_ack_loss_stays_stopped(self):
        with self.assertRaises(r.RemoteStop):self.transport('POST','/repos/'+r.REPO+'/git/commits',{'tree':'a'*40})
        with self.assertRaisesRegex(r.RemoteStop,'operator review'):self.transport.recover_readonly()
        self.assertEqual(len(self.writes),1)
