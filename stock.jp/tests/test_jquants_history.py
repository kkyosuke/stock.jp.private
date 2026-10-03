from datetime import date,timedelta
import json
from pathlib import Path
import tempfile
import unittest
from scripts import jquants_history as h, jquants_ingest as i, market_data_git as codec


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.private=self.root/'operations/private';(self.private/'market-data/daily-prices').mkdir(parents=True)
        config={'jquants':{'enabled':True,'plan':'light','addons':[],'base_url':i.BASE_URL,'api_key_env':'JQUANTS_API_KEY','ingestion':{'enabled':True,'max_requests_per_run':12,'archive_root':'operations/private/market-data/jquants'}},'price_source':{'enabled':True,'provider':'jquants_v2_private_archive','storage':'private_local','archive_root':'operations/private/market-data/daily-prices'}}
        (self.private/'source-config.json').write_text(json.dumps(config));self.calls=[];self.end=date(2026,10,2)
    def transport(self,endpoint,query):
        self.calls.append((endpoint,query))
        if endpoint in ('/markets/calendar','/indices/bars/daily/topix'):
            start=date.fromisoformat(query['from']);end=date.fromisoformat(query['to']);rows=[]
            while start<=end:
                if endpoint=='/markets/calendar':rows.append({'Date':str(start),'HolDiv':'1' if start.weekday()<5 else '0'})
                elif start.weekday()<5:rows.append({'Date':str(start),'O':100,'H':101,'L':99,'C':100})
                start+=timedelta(days=1)
        elif endpoint=='/equities/master':rows=[{'Date':query['date'],'Code':'12340','Mkt':'0111','ProdCat':'011'}]
        else:rows=[{'Date':query['date'],'Code':'12340','O':100,'H':101,'L':99,'C':100,'Vo':100,'Va':10000}]
        return {'data':rows}
    def run_history(self,**kwargs):
        return h.run(self.root,as_of=self.end,sessions=kwargs.pop('sessions',8),transport=kwargs.pop('transport',self.transport),sleep=lambda _:None,**kwargs)
    def test_bounds_resume_and_roundtrip(self):
        first=self.run_history();self.assertEqual(first['status'],'BATCH_EXHAUSTED');self.assertEqual(len(self.calls),12)
        self.assertEqual(self.run_history(),first);self.assertEqual(len(self.calls),12)
        store=self.root/'store';codec.pack(self.private/'market-data',store);codec.restore(store,self.root/'restored')
        second=self.run_history(batch=2);self.assertEqual(second['status'],'COMPLETE');self.assertEqual(len(self.calls),18)
        self.assertEqual(self.run_history(batch=2),second);self.assertEqual(len(self.calls),18)
        with self.assertRaises(i.IngestionError):self.run_history(sessions=9,batch=2)
    def test_failure_is_charged_and_no_retry(self):
        def failed(endpoint,query):self.calls.append(1);raise i.ApiError(503,'api_error')
        self.assertEqual(self.run_history(transport=failed)['status'],'PAUSED');self.assertEqual(len(self.calls),1)
        self.assertEqual(self.run_history()['requests_used'],12)
    def test_denial_blocks_and_cannot_advance(self):
        def denied(endpoint,query):raise i.ApiError(403,'access_denied_unclassified')
        r=self.run_history(transport=denied);self.assertEqual(r['status'],'BLOCKED');self.assertEqual(r['requests_used'],1)
        self.assertEqual(self.run_history(batch=2),r)
    def test_429_cooldown(self):
        def limited(endpoint,query):raise i.ApiError(429,'api_error')
        r=self.run_history(transport=limited);self.assertEqual(r['status'],'PAUSED')
        self.assertEqual(self.run_history(),r);self.assertEqual(self.calls,[])
    def test_missing_journal_never_resets_budget(self):
        self.run_history();(self.private/'market-data/jquants/history/state.json').unlink()
        count=len(self.calls)
        with self.assertRaises(ValueError):self.run_history()
        self.assertEqual(count,len(self.calls))
    def test_calendar_missing_day_blocks(self):
        def gap(endpoint,query):
            payload=self.transport(endpoint,query)
            if endpoint=='/markets/calendar':payload['data'].pop(10)
            return payload
        self.assertEqual(self.run_history(transport=gap)['status'],'BLOCKED');self.assertEqual(len(self.calls),1)
    def test_maximum_campaign_280_sessions(self):
        for batch in range(1,48):result=self.run_history(sessions=280,batch=batch)
        self.assertEqual(result['status'],'COMPLETE');self.assertEqual(result['requests_used'],562)
        self.assertEqual(result['batch'],47)
        self.assertEqual(len(list((self.private/'market-data/daily-prices').glob('*/*.csv'))),280)
    def test_all_600_failure_reservations_survive_batches(self):
        def failed(endpoint,query):raise i.ApiError(503,'api_error')
        for batch in range(1,51):
            for _ in range(12):result=self.run_history(batch=batch,transport=failed)
        self.assertEqual(result['status'],'EXHAUSTED');self.assertEqual(result['requests_used'],600)
        self.assertEqual(self.run_history(batch=50),result)
    def test_raw_page_tamper_stops_before_network(self):
        self.run_history();p=next((self.private/'market-data/jquants/objects').glob('*/*.json'));p.write_text('{}')
        count=len(self.calls)
        with self.assertRaises(ValueError):self.run_history(batch=2)
        self.assertEqual(len(self.calls),count)
    def test_crash_after_reservation_still_charged(self):
        def crash(endpoint,query):raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.run_history(transport=crash)
        state=json.loads((self.private/'market-data/jquants/history/state.json').read_text())
        self.assertEqual(sum(map(len,state['batches'])),1)
        self.assertEqual(self.run_history()['requests_used'],12)
    def test_daily_lock_prevents_history_writer(self):
        with h.daily.workspace_lock(self.private):
            with self.assertRaises(i.IngestionError):self.run_history()
        self.assertEqual(self.calls,[])
    def test_pagination_checkpoint_across_batches(self):
        p=self.private/'source-config.json';config=json.loads(p.read_text());config['jquants']['ingestion']['max_requests_per_run']=1;p.write_text(json.dumps(config))
        def paged(endpoint,query):
            payload=self.transport(endpoint,query)
            if endpoint=='/markets/calendar':
                rows=payload['data']
                return {'data':rows[300:]} if query.get('pagination_key') else {'data':rows[:300],'pagination_key':'second'}
            return payload
        for batch in range(1,20):result=self.run_history(batch=batch,transport=paged)
        self.assertEqual(result['status'],'COMPLETE');self.assertEqual(result['requests_used'],19)
    def test_existing_daily_partition_reused(self):
        archive=self.private/'market-data/jquants';client=i.Client(plan='light',transport=self.transport,sleep=lambda _:None,retries=0)
        for name in ('equities_master','equities_daily'):i.collect_partition(archive,h.BY_NAME[name],self.end,client)
        self.calls.clear();self.run_history();result=self.run_history(batch=2)
        self.assertEqual(result['status'],'COMPLETE');self.assertEqual(result['requests_used'],16)
    def test_partial_history_is_publishable_with_explicit_status(self):
        import subprocess
        from scripts import daily_publication as publication
        self.run_history()
        source=self.private/'market-data';(source/'jquants/latest.json').write_text(json.dumps(i.planned_status('light',(),self.end)))
        def git(*args):return subprocess.check_output(['git','-C',str(self.root),*args],stderr=subprocess.DEVNULL,text=True).strip()
        git('init');git('config','user.email','synthetic@example.test');git('config','user.name','Synthetic')
        git('commit','--allow-empty','-m','base');base=git('rev-parse','HEAD')
        codec.pack(source,self.root/'stock.jp/data/jquants-archive');git('add','stock.jp/data');git('commit','-m','bounded history')
        proof=publication.validate_local(self.root,'market','2026-10-02',base,git('rev-parse','HEAD'))
        self.assertEqual(proof['history_status'],'BATCH_EXHAUSTED')

    def test_final_reservation_crash_can_resume_same_or_next_batch(self):
        for limit in (1,12):
            for inspect_same_batch in (True,False):
                with self.subTest(limit=limit,inspect_same_batch=inspect_same_batch):
                    self.setUp()
                    config_path=self.private/'source-config.json'
                    config=json.loads(config_path.read_text());config['jquants']['ingestion']['max_requests_per_run']=limit
                    config_path.write_text(json.dumps(config))
                    attempts=0
                    def crash_last(endpoint,query):
                        nonlocal attempts
                        attempts+=1
                        if attempts==limit:raise KeyboardInterrupt()
                        return self.transport(endpoint,query)
                    with self.assertRaises(KeyboardInterrupt):self.run_history(transport=crash_last)
                    path=self.private/'market-data/jquants/history/state.json'
                    before=json.loads(path.read_text())
                    self.assertEqual(before['status'],'RUNNING');self.assertEqual(len(before['batches'][0]),limit)
                    if inspect_same_batch:
                        calls=len(self.calls);paused=self.run_history()
                        self.assertEqual(paused['status'],'PAUSED');self.assertEqual(len(self.calls),calls)
                        after=json.loads(path.read_text());self.assertEqual(after['batches'],before['batches'])
                        self.assertEqual(after['tasks'],before['tasks']);self.assertEqual(after['ranges'],before['ranges'])
                        self.assertEqual(self.run_history(),paused)
                    resumed=self.run_history(batch=2)
                    self.assertIn(resumed['status'],('COMPLETE','BATCH_EXHAUSTED'))
                    after=json.loads(path.read_text());self.assertEqual(after['batches'][0],before['batches'][0])
                    self.assertEqual(len(after['batches']),2);self.assertLessEqual(len(after['batches'][1]),limit)
                    self.assertGreater(resumed['requests_used'],limit)

    def test_last_campaign_reservation_crash_stops_without_refund(self):
        def crash(endpoint,query):raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.run_history(transport=crash)
        path=self.private/'market-data/jquants/history/state.json';state=json.loads(path.read_text())
        # Fixture represents the same persisted reservation boundary in batch 50.
        receipt=state['batches'][0][0];state['batches']=[[dict(receipt) for _ in range(12)] for _ in range(50)]
        path.write_text(json.dumps(state))
        result=self.run_history(batch=50)
        self.assertEqual(result['status'],'EXHAUSTED');self.assertEqual(result['requests_used'],600)
        self.assertEqual(json.loads(path.read_text())['batches'],state['batches']);self.assertEqual(self.calls,[])

    def add_interrupted_correction(self):
        archive=self.private/'market-data/jquants'
        def correction(endpoint,query):
            return {'data':[{'Date':str(self.end),'Code':'12340','O':200,'H':201,'L':199,'C':200,'Vo':100,'Va':20000}],
                    'pagination_key':'correction-next'}
        client=i.Client(plan='light',transport=correction,max_requests=1,sleep=lambda _:None,retries=0)
        with self.assertRaises(i.RequestBudgetExhausted):i.collect_partition(archive,h.BY_NAME['equities_daily'],self.end,client)
        return archive/'datasets/equities_daily'/str(self.end)/'checkpoint.json'

    def test_unfinished_correction_takes_priority_over_old_latest(self):
        import csv
        archive=self.private/'market-data/jquants';client=i.Client(plan='light',transport=self.transport,sleep=lambda _:None,retries=0)
        for name in ('equities_master','equities_daily'):i.collect_partition(archive,h.BY_NAME[name],self.end,client)
        checkpoint=self.add_interrupted_correction();self.calls.clear()
        def resume(endpoint,query):
            if endpoint=='/equities/bars/daily':
                self.calls.append((endpoint,query));self.assertEqual(query['pagination_key'],'correction-next')
                return {'data':[]}
            return self.transport(endpoint,query)
        result=self.run_history(sessions=1,transport=resume)
        self.assertEqual(result['status'],'COMPLETE');self.assertEqual(result['requests_used'],3)
        self.assertFalse(checkpoint.exists())
        self.assertEqual(i.partition_rows(archive,'equities_daily',self.end)[0]['C'],200)
        with (self.private/'market-data/daily-prices/2026/2026-10-02.csv').open() as stream:
            self.assertEqual(next(csv.DictReader(stream))['終値'],'200')

    def test_completed_history_cannot_hide_new_unfinished_correction(self):
        self.assertEqual(self.run_history(sessions=1)['status'],'COMPLETE')
        checkpoint=self.add_interrupted_correction();count=len(self.calls)
        with self.assertRaises(ValueError):self.run_history(sessions=1)
        self.assertTrue(checkpoint.exists());self.assertEqual(len(self.calls),count)
    def test_low_disk_pauses_before_request_and_keeps_measurement(self):
        from unittest.mock import patch
        from scripts import history_resources as resources
        original=resources.inspect
        def low_disk(*args):
            report=original(*args);report['free_bytes']=resources.MIN_FREE-1;return report
        with patch.object(resources,'inspect',side_effect=low_disk):result=self.run_history()
        self.assertEqual(result['status'],'PAUSED');self.assertEqual(result['requests_used'],0);self.assertEqual(self.calls,[])
        state=json.loads((self.private/'market-data/jquants/history/state.json').read_text())
        self.assertEqual(state['resource_checks']['1']['free_bytes'],resources.MIN_FREE-1)
    def test_response_cap_rejects_before_archiving(self):
        from unittest.mock import patch
        with patch.object(h.resources,'MAX_RESPONSE',128):result=self.run_history()
        self.assertEqual(result['status'],'BLOCKED');self.assertEqual(result['requests_used'],1)
        self.assertEqual(list((self.private/'market-data/jquants/objects').glob('*/*.json')),[])
    def test_full_market_scale_one_session(self):
        import resource
        def full_market(endpoint,query):
            if endpoint=='/equities/master':return {'data':[{'Date':str(self.end),'Code':f'{n}0','Mkt':'0111','ProdCat':'011',
                'CoName':'Synthetic full-market company','CoNameEn':'Synthetic Example','MktNm':'Prime','S33Nm':'Synthetic sector'} for n in range(1000,7000)]}
            if endpoint=='/equities/bars/daily':return {'data':[{'Date':str(self.end),'Code':f'{n}0','O':100,'H':101,'L':99,'C':100,
                'Vo':1000,'Va':100000,'AdjO':100,'AdjH':101,'AdjL':99,'AdjC':100,'AdjVo':1000,'AdjFactor':1} for n in range(1000,7000)]}
            return self.transport(endpoint,query)
        result=self.run_history(sessions=1,transport=full_market)
        self.assertEqual(result['status'],'COMPLETE')
        manifest=json.loads((self.private/'market-data/daily-prices/latest.json').read_text())
        self.assertEqual(manifest['universe']['count'],6000)
        # Linux CI peak RSS includes the whole unittest process, not just this run.
        self.assertLess(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,h.resources.MAX_ADDRESS_SPACE)

    def test_wire_response_read_is_bounded(self):
        from unittest.mock import MagicMock
        from scripts.jquants_ingest import Client,ApiError
        client=Client(plan='light',max_response_bytes=128,transport=lambda *_:None);client.transport=None
        response=MagicMock();response.status=200;response.read.return_value=b' '*129
        client.opener.open=MagicMock();client.opener.open.return_value.__enter__.return_value=response
        with self.assertRaises(ApiError):client._request('/equities/master',{})
        response.read.assert_called_once_with(129)
    def test_raw_and_staging_caps_stop_before_request(self):
        from unittest.mock import patch
        original=h.resources.inspect
        for field,value in (('raw_bytes',h.resources.MAX_RAW),('staging_bytes',h.resources.MAX_STAGING+1)):
            def oversized(*args):
                report=original(*args);report[field]=value;return report
            with patch.object(h.resources,'inspect',side_effect=oversized):result=self.run_history()
            self.assertEqual(result['status'],'PAUSED');self.assertEqual(result['requests_used'],0)
            self.assertEqual(self.calls,[])

    def test_completed_campaign_recovers_only_exact_published_checkpoint(self):
        result=self.run_history(sessions=1);count=len(self.calls)
        archive=self.private/'market-data/jquants'
        directory=archive/'datasets/equities_daily'/str(self.end)
        manifest=json.loads((directory/'latest.json').read_text())
        checkpoint={key:manifest[key] for key in ('query','started_at','pages')}
        checkpoint.update(endpoint='/equities/bars/daily',next_key=None,seen_keys=[],complete=True)
        path=directory/'checkpoint.json'
        path.write_text(json.dumps(checkpoint))
        before={str(p.relative_to(archive)):p.read_bytes() for p in archive.rglob('*') if p.is_file() and p.name not in ('checkpoint.json','.ingestion.lock')}
        self.assertEqual(self.run_history(sessions=1),result);self.assertFalse(path.exists())
        self.assertEqual(len(self.calls),count)
        self.assertEqual(before,{str(p.relative_to(archive)):p.read_bytes() for p in archive.rglob('*') if p.is_file() and p.name not in ('checkpoint.json','.ingestion.lock')})
        checkpoint['started_at']='2026-10-03T00:00:01+09:00'
        path.write_text(json.dumps(checkpoint))
        with self.assertRaises(ValueError):self.run_history(sessions=1)
        self.assertTrue(path.exists());self.assertEqual(len(self.calls),count)
