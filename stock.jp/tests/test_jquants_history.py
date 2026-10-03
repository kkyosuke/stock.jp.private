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
