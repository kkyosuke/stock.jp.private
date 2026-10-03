"""No-network publication guards after checkpoint writes, pacing and known retries."""
import copy
from datetime import datetime,timedelta,timezone
import unittest
from unittest.mock import patch
from scripts import actions_intent as intent, actions_rate as rate, actions_attestation as attest
from scripts import actions_driver as driver, actions_daily_remote as remote
from test_actions_daily_remote import authorization
import test_actions_driver as integration

PREFIX='/repos/'+remote.REPO
OPERATIONS=[('POST','/git/blobs',{}),('POST','/git/trees',{}),('POST','/git/commits',{}),
    ('POST','/git/refs',{'ref':'refs/heads/daily/market/2026-10-02','sha':'a'*40}),
    ('PATCH','/git/refs/heads/daily/market/2026-10-02',{'sha':'a'*40,'force':False}),
    ('POST','/pulls',{'head':'daily/market/2026-10-02','base':'main'}),
    ('PUT','/pulls/1/merge',{'sha':'a'*40,'merge_method':'squash'})]

class BoundaryTests(unittest.TestCase):
    def scenario(self,operation,where,failure):
        clock=[10000.];bad=[False];events=[];attempts=[]
        scope=dict(old_scheduler_id='old',old_writer='owner',old_lease_id='lease',handover_epoch='epoch',
                   writer='actions-v1',expires_at=datetime.fromtimestamp(30000,timezone.utc).isoformat())
        def now():return datetime.fromtimestamp(clock[0],timezone.utc)
        def probe(identity):
            events.append(('live',clock[0]))
            if bad[0] and failure=='unknown':raise OSError('synthetic offline controller')
            result={**identity,'controller':'chatgpt-automations','observed_by':'kkyosuke','enabled':False,
                    'active_run_ids':[],'lease_state':'released','lease_owner':None,'observed_at':now().isoformat()}
            if bad[0]:
                if failure=='revived':result.update(enabled=True,active_run_ids=['old-run'])
                elif failure=='lease':result.update(lease_state='active',lease_owner='owner')
                elif failure=='stale':result['observed_at']=(now()-timedelta(seconds=61)).isoformat()
                elif failure=='expired':scope['expires_at']=now().isoformat()
                elif failure=='facts_expired':checkpoint.authorization['expires_at']=now().isoformat()
                elif failure=='gate':checkpoint.authorization['publication_authorized']=False
            return result
        def live():return attest.verify_live(scope,live_probe=probe,clock=now)
        def sleep(seconds):
            clock[0]+=seconds
            if where in {'headroom','pacing','retry'} and seconds>0:bad[0]=True
        class Checkpoint:
            def __init__(self):
                self.clock=now
                self.authorization={**authorization(),'expires_at':scope['expires_at'],'handover_check':live,'publication_io_check':live,
                    'merge_io_prepare':lambda:events.append(('merge-evidence',clock[0])),
                    'merge_io_check':lambda:None}
                self.document={'transaction':dict(head='a'*40,tree='b'*40,generation='c'*64)};self.saved=[]
            def commit(self):
                self.saved.append(copy.deepcopy(self.document));events.append(('persist',clock[0]))
                if where=='checkpoint':clock[0]+=120;bad[0]=True
                if where=='pacing':budget.last_write=clock[0]
        checkpoint=Checkpoint()
        def request(method,path,body):
            attempts.append((method,path));events.append(('send',clock[0]))
            if where=='retry' and len(attempts)==1:raise rate.RateLimited(clock[0]+120)
            return dict(sha='a'*40,number=1,merged=True,object={'sha':'a'*40})
        budget=rate.BudgetedTransport(request,clock=lambda:clock[0],sleep=sleep)
        if where=='headroom':budget.reads=budget.READ_LIMIT-79
        method,path,body=operation
        if failure=='healthy':
            intent.IntentTransport(checkpoint,budget)(method,PREFIX+path,body)
            self.assertEqual(len(attempts),2 if where=='retry' else 1)
            self.assertEqual(events[-2][0],'send')
            self.assertEqual(events[-3][0],'live')
            self.assertEqual(checkpoint.saved[-1]['publication_intent']['state'],'ACKED')
        else:
            with self.assertRaises(remote.RemoteStop):intent.IntentTransport(checkpoint,budget)(method,PREFIX+path,body)
            self.assertEqual(len(attempts),1 if where=='retry' else 0)
            if checkpoint.saved:self.assertEqual(checkpoint.saved[-1]['publication_intent']['state'],'PENDING')

    def test_all_publication_mutations_fail_closed_at_every_delay(self):
        for operation in OPERATIONS:
            for where in ('headroom','checkpoint','pacing','retry'):
                for failure in ('revived','lease','stale','expired','unknown','facts_expired','gate'):
                    with self.subTest(operation=operation[:2],where=where,failure=failure):
                        self.scenario(operation,where,failure)

    def test_healthy_observation_is_last_before_each_mutation(self):
        for operation in OPERATIONS:
            for where in ('headroom','checkpoint','pacing','retry'):
                with self.subTest(operation=operation[:2],where=where):self.scenario(operation,where,'healthy')

class MergeBoundaryTests(unittest.TestCase):
    def run_scenario(self,change,when):
        fixture=integration.DriverTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        clock=[fixture.fixture.at.timestamp()];armed=[False];changed=[False];attempts=[]
        start=clock[0]
        def now():return datetime.fromtimestamp(clock[0],fixture.fixture.at.tzinfo)
        def change_evidence():
            changed[0]=True
            if change=='head':fixture.remote.prs[1]['head']['sha']=fixture.base
            elif change=='base':fixture.remote.refs['heads/main']='e'*40
            elif change=='ci':fixture.remote.ci_ready=False
            elif change=='review':fixture.remote.reviews=[dict(id=1,user={'id':1},state='CHANGES_REQUESTED',
                commit_id=fixture.remote.prs[1]['head']['sha'],submitted_at=datetime.now(timezone.utc).isoformat())]
        def wait(seconds):
            clock[0]+=seconds
            if armed[0] and seconds>60 and not changed[0] and when not in {'evidence_delay','short_evidence_delay'}:change_evidence()
        def request(method,path,body):
            if method=='PUT' and path.endswith('/merge'):
                attempts.append(path)
                if when=='retry' and len(attempts)==1:raise rate.RateLimited(clock[0]+120)
            if armed[0] and when in {'evidence_delay','short_evidence_delay'} and '/reviews?' in path and not changed[0]:
                changed[0]=True
                fixture.remote.ci_ready=False
                raise rate.RateLimited(clock[0]+(20 if when=='short_evidence_delay' else 120))
            return fixture.remote(method,path,body)
        request.review_decision=fixture.remote.review_decision
        class Transport(rate.BudgetedTransport):
            def mutation(self,method,path,body,**kwargs):
                if path.endswith('/merge'):
                    armed[0]=True
                    if when=='pacing':self.reads=self.READ_LIMIT
                return super().mutation(method,path,body,**kwargs)
        transport=Transport(request,clock=lambda:clock[0],sleep=wait,deadline=start+5*3600)
        def live(identity):
            if armed[0] and when=='slow_live' and not changed[0]:
                clock[0]+=120;changed[0]=True
            return {**fixture.controller_observation,'observed_at':now().isoformat(),
                    **({'enabled':True} if changed[0] and change=='controller' else {})}
        with patch.object(driver.daily.ingest,'utc_now',side_effect=lambda:now().isoformat()):
            with self.assertRaises((remote.RemoteStop,ValueError)):
                driver.Driver(fixture.root,transport,fixture.fixture.transport,pin=fixture.remote.pin,writer='actions-v1',
                    context=fixture.context,clock=now,sleep=lambda _:None,live_handover_probe=live).execute('daily')
        self.assertTrue(changed[0]);self.assertEqual(fixture.remote.merge_writes,0)
        self.assertEqual(len(attempts),1 if when=='retry' else 0)

    def test_merge_rechecks_head_base_ci_reviews_after_wait(self):
        for change in ('head','base','ci','review','controller'):
            with self.subTest(change=change):self.run_scenario(change,'pacing')

    def test_merge_rechecks_after_known_rate_rejection(self):
        for change in ('head','base','ci','review','controller'):
            with self.subTest(change=change):self.run_scenario(change,'retry')

    def test_slow_final_live_observation_expires_merge_proof(self):
        self.run_scenario('ci','slow_live')

    def test_rate_wait_during_evidence_fetch_forces_new_snapshot(self):
        for when in ('evidence_delay','short_evidence_delay'):
            with self.subTest(when=when):self.run_scenario('ci',when)


class ProviderBoundaryTests(unittest.TestCase):
    def test_same_day_expiry_and_gate_change_after_reservation_keep_charge(self):
        import test_actions_daily_remote as daily_fixture
        import test_actions_execution as history_fixture
        for mode in ('daily','history'):
            for failure in ('expiry','gate'):
                with self.subTest(mode=mode,failure=failure):
                    fixture=(daily_fixture.RemoteDailyTests() if mode=='daily' else history_fixture.HistoryRemoteTests())
                    fixture.setUp();self.addCleanup(fixture.doCleanups)
                    start=(datetime.now(timezone.utc)+timedelta(days=1)).replace(hour=12,minute=0,second=0,microsecond=0)
                    clock=[start];expiry=start+timedelta(seconds=2)
                    adapter=fixture.adapter() if mode=='daily' else fixture.remote()
                    adapter.clock=lambda:clock[0]
                    adapter.authorization['expires_at']=expiry.isoformat()
                    if mode=='daily':fixture.at=start
                    original=adapter.commit
                    def commit():
                        original()
                        run=(adapter.document.get('runs',{}).get(adapter.day) if mode=='daily' else adapter.document.get('history'))
                        if run and run['phase']=='IN_FLIGHT':
                            clock[0]=expiry+timedelta(seconds=1)
                            if failure=='gate':
                                adapter.authorization.pop('expires_at')
                                adapter.authorization['publication_authorized']=False
                    with patch.object(adapter,'commit',side_effect=commit):
                        with self.assertRaises(remote.RemoteStop):
                            if mode=='daily':fixture.run_daily(remote=adapter)
                            else:fixture.run_history(remote=adapter)
                    self.assertFalse(fixture.calls)
                    document=fixture.store.read()[1]
                    run=document['runs'][start.date().isoformat()] if mode=='daily' else document['history']
                    self.assertEqual(run['phase'],'IN_FLIGHT');self.assertEqual(len(run['reservation_ids']),1)
                    self.assertEqual(clock[0].date(),start.date())


class EvidenceReservationTests(unittest.TestCase):
    def test_short_quota_wait_after_evidence_invalidates_snapshot(self):
        clock=[3590.];current=[True];prepared=[];sent=[]
        def wait(seconds):
            clock[0]+=seconds
            if seconds:current[0]=False
        client=rate.BudgetedTransport(lambda *a:sent.append(a),clock=lambda:clock[0],sleep=wait)
        def prepare():
            prepared.append(current[0])
            remote.require(current[0],'changed merge evidence')
            client.reads=client.READ_LIMIT
        with self.assertRaisesRegex(remote.RemoteStop,'changed merge evidence'):
            client.mutation('PUT',PREFIX+'/pulls/1/merge',{},before_io=lambda:None,prepare_io=prepare)
        self.assertEqual(prepared,[True,False]);self.assertFalse(sent)
