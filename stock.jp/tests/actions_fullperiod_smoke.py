"""Opt-in offline history → adoption → fresh-daily driver smoke test.

Run from stock.jp: PYTHONPATH=.:tests python tests/actions_fullperiod_smoke.py
All GitHub/provider/controller I/O uses synthetic fixtures. Rate-limit waits advance
an injected clock; no workflow dispatch, credentials, real acquisition or merge.
This intentionally exercises the actual 280-session, 47-batch runner and can take
tens of minutes. The regular unittest suite covers smaller regression scenarios.
"""
import copy,json
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
import test_actions_driver as f
import test_jquants_history as hf
from scripts import actions_rate as rate, actions_driver as d, actions_authorization as auth, market_data_git as codec
from scripts.git_sandbox import git

def main():
    fixture=f.DriverTests();fixture.setUp()
    history=hf.HistoryTest();history.setUp()
    try:
        fixture.scope['history_campaign']['sessions']=280
        # Scope changes here are only synthetic fixture construction, before any run.
        fixture.scope['expires_at']=(fixture.now+timedelta(days=7)).isoformat()
        codec.atomic(fixture.root/auth.MANIFEST,codec.encoded(fixture.scope));git(fixture.root,'add','.github')
        main=fixture.commit('Synthetic approved full-period scope')
        fixture.remote.refs['heads/main']=main;fixture.context['sha']=main
        fixture.remote.pin=codec.digest(codec.encoded(fixture.scope));fixture.remote.store_bodies=False
        clock=[fixture.fixture.at.timestamp()];started=clock[0]
        def now():return datetime.fromtimestamp(clock[0],d.ZoneInfo('Asia/Tokyo'))
        def sleep(seconds):clock[0]+=seconds
        class Controller:
            def __init__(self):self.reads=0
            def read(self,identity):
                self.reads+=1
                return {**copy.deepcopy(fixture.controller_observation),'observed_at':now().isoformat()}
        controller=Controller();segments=[]
        for segment in range(1,21):
            request=rate.BudgetedTransport(fixture.remote,clock=lambda:clock[0],sleep=sleep)
            count=len(fixture.remote.calls);start=clock[0]
            driver=d.Driver(fixture.root,request,history.transport,pin=fixture.remote.pin,writer='actions-v1',context=fixture.context,
                            clock=now,sleep=lambda _:None,live_handover_probe=controller.read)
            with patch.object(d.daily.ingest,'utc_now',side_effect=lambda:now().isoformat()):result=driver.execute('history')
            item=dict(segment=segment,status=result['status'],provider_calls=len(history.calls),github_rest_calls=len(fixture.remote.calls)-count,
                      simulated_elapsed_seconds=round(clock[0]-start,1));segments.append(item);print(json.dumps(item),flush=True)
            if result['status']=='RESTORED':break
            assert result['status']=='PAUSED_GITHUB_BUDGET',result
            clock[0]=(int(clock[0]//3600)+1)*3600
        assert result['status']=='RESTORED',result
        assert len(history.calls)==562
        fixture.sync_main()
        # A fresh daily workflow starts at 21:00 after history adoption.
        daily_start=now().replace(hour=21,minute=0,second=0,microsecond=0)
        if daily_start<=now():daily_start+=timedelta(days=1)
        clock[0]=daily_start.timestamp();fixture.fixture.at=daily_start
        request=rate.BudgetedTransport(fixture.remote,clock=lambda:clock[0],sleep=sleep)
        count=len(fixture.remote.calls);start=clock[0]
        with patch.object(d.daily.ingest,'utc_now',side_effect=lambda:now().isoformat()):
            result=d.Driver(fixture.root,request,fixture.fixture.transport,pin=fixture.remote.pin,writer='actions-v1',
                context=fixture.context,clock=now,sleep=lambda _:None,live_handover_probe=controller.read).execute('daily')
        print(json.dumps(dict(fresh_daily=result,provider_calls=len(fixture.fixture.calls),github_rest_calls=len(fixture.remote.calls)-count,
                              simulated_elapsed_seconds=round(clock[0]-start,1))),flush=True)
        assert result['status']=='RESTORED',result
        store=fixture.root/'stock.jp/data/jquants-archive'
        print(json.dumps(dict(history_segments=len(segments),history_requests=len(history.calls),
            adopted_archive_objects=len(list((store/'objects').rglob('*.gz'))),controller_reads=controller.reads,
            total_github_rest_calls=len(fixture.remote.calls),total_simulated_seconds=round(clock[0]-started,1))),flush=True)
    finally:
        history.doCleanups();fixture.doCleanups()


if __name__ == "__main__":
    main()
