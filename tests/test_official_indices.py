from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stock_jp.market_data.jquants import ingest, index_history, daily
from stock_jp.market_data.official_indices import load_index_series, OBSERVATION
from stock_jp.market_data.regime_sources import MarketRegimeSourceError
from tests import test_jquants_daily as daily_helpers


class StandardDailyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = daily_helpers.DailyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.config['jquants']['plan'] = 'standard'
        self.fixture.save_config()

    def transport(self, endpoint, params):
        if endpoint == '/indices/bars/daily':
            self.fixture.calls.append((endpoint, dict(params)))
            return {'data':[{'Code':'0070','Date':params['date'],'C':100}]}
        return self.fixture.transport(endpoint, params)

    def test_standard_ten_calls_and_idempotent_restore_validation(self):
        f = self.fixture
        r = f.run_daily(transport=self.transport)
        self.assertEqual((r['status'], r['requests_used']), ('COLLECTED',10))
        self.assertEqual(f.run_daily(transport=self.transport), r)
        self.assertEqual(len(f.calls), 10)
        from stock_jp.market_data.jquants.validation import validate_archive
        names = {p.relative_to(f.archive.parent).as_posix() for p in f.archive.rglob('*') if p.is_file()}
        self.assertTrue(validate_archive(names, lambda n:(f.archive.parent/n).read_bytes()))

    def test_missing_growth_is_not_complete(self):
        def other(e,p):
            if e == '/indices/bars/daily':return {'data':[{'Code':'0000','Date':p['date'],'C':200}]}
            return self.transport(e,p)
        r=self.fixture.run_daily(transport=other)
        self.assertNotEqual(r['status'],'COLLECTED')

    def test_paging_budget_is_shared_and_no_thirteenth_request(self):
        def pages(e,p):
            if e == '/indices/bars/daily':
                self.fixture.calls.append((e,dict(p)))
                key=p.get('pagination_key','0')
                return {'data':[], 'pagination_key':str(int(key)+1)}
            return self.transport(e,p)
        r=self.fixture.run_daily(transport=pages)
        self.assertEqual((r['status'],r['requests_used']),('BUDGET_EXHAUSTED',12))
        self.assertEqual(self.fixture.run_daily(transport=pages),r)
        self.assertEqual(len(self.fixture.calls),12)

    def test_before_publication_and_holiday_no_index_call(self):
        f=self.fixture
        f.at=datetime.fromisoformat('2026-10-02T20:59:59+09:00')
        self.assertEqual(f.run_daily(transport=self.transport)['status'],'WAIT_PUBLICATION')
        self.assertFalse(f.calls)
        f.at=datetime.fromisoformat('2026-10-02T21:00:00+09:00')
        f.run_daily(transport=self.transport)
        f.calls.clear();f.at=datetime.fromisoformat('2026-10-03T21:00:00+09:00')
        self.assertEqual(f.run_daily(transport=self.transport)['status'],'HOLIDAY')
        self.assertNotIn('/indices/bars/daily',[x[0] for x in f.calls])

    def test_bootstrap_shares_daily_budget_and_is_not_repeated(self):
        f=self.fixture
        start=date(2025,11,1);end=date(2026,10,1)
        rows=[];day=start
        while day<=end:
            if day.weekday()<5:rows.append({'Code':'0070','Date':str(day),'C':100})
            day+=timedelta(days=1)
        f.config['jquants']['index_bootstrap']={'from':str(start),'through':str(end),'run_date':'2026-10-02','max_requests':3}
        f.save_config()
        def transport(endpoint,params):
            if endpoint=='/indices/bars/daily' and 'from' in params:
                f.calls.append((endpoint,dict(params)));return {'data':rows}
            return self.transport(endpoint,params)
        result=f.run_daily(transport=transport)
        self.assertEqual((result['status'],result['requests_used']),('COLLECTED',11))
        self.assertEqual(f.run_daily(transport=transport),result)
        self.assertEqual(len(f.calls),11)
        document=json.loads((f.archive/index_history.PATH).read_text())
        self.assertEqual(len(document['attempts']),1)
        self.assertTrue(document['complete'])

    def test_legacy_observation_is_hash_checked_and_timestamped_once(self):
        from stock_jp.market_data.official_indices import observe_legacy
        f=self.fixture
        rows=[{'Date':'2026-10-01','C':100}]
        ranges={}
        for kind,data in [('topix_daily',rows),('trading_calendar',[{'Date':'2026-10-01','HolDiv':'1'}])]:
            ranges[kind]={'complete':True,'query':{'from':'2026-10-01','to':'2026-10-01'},'pages':[ingest.save_object(f.archive,{'data':data})]}
        ingest.atomic_json(f.archive/'history/state.json',{'status':'COMPLETE','ranges':ranges})
        with patch.object(ingest,'utc_now',return_value='2026-10-02T22:00:00+09:00'):
            first=observe_legacy(f.archive)
        with patch.object(ingest,'utc_now',side_effect=AssertionError('do not rewrite provenance')):
            self.assertEqual(observe_legacy(f.archive),first)
        self.assertEqual(first['observed_at'],'2026-10-02T22:00:00+09:00')


class IndexEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.archive=self.root/'.work/market-data/jquants'
        self.archive.mkdir(parents=True)
        self.end=date(2026,9,30);self.start=self.end-timedelta(days=310)
        self.calendar=[];self.rows=[]
        day=self.start
        while day<=self.end:
            opened=day.weekday()<5
            self.calendar.append({'Date':str(day),'HolDiv':'1' if opened else '0'})
            if opened:self.rows.append({'Code':'0070','Date':str(day),'C':100+len(self.rows)})
            day+=timedelta(days=1)
        self.approval={'from':str(self.start),'through':str(self.end),'max_requests':3,'run_date':'2026-10-01'}
        self.day=date(2026,10,1)
        self.observed='2026-10-01T01:00:00+09:00'
        self.calls=[]

    def collect(self, transport=None):
        def call(e,p):self.calls.append((e,p));return {'data':self.rows}
        client=ingest.Client(plan='standard',max_requests=12,retries=0,transport=transport or call,sleep=lambda _:None)
        with patch.object(ingest,'utc_now',return_value=self.observed):
            return index_history.collect(self.archive,client,day=self.day,approval=self.approval,persist=lambda:None)

    def legacy(self):
        ranges={}
        for kind,rows in [('topix_daily',[{k:v for k,v in r.items() if k!='Code'} for r in self.rows]),('trading_calendar',self.calendar)]:
            ranges[kind]={'complete':True,'query':{'from':str(self.start),'to':str(self.end)},'pages':[ingest.save_object(self.archive,{'data':rows})]}
        ingest.atomic_json(self.archive.parent/OBSERVATION,{'version':1,'observed_at':self.observed,'ranges':ranges})

    def test_range_one_request_resume_and_exact_200(self):
        self.collect();self.collect();self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0][1],{'code':'0070','from':str(self.start),'to':str(self.end)})
        self.legacy()
        cutoff=datetime.fromisoformat('2026-09-30T23:59:59+09:00')
        result=load_index_series(self.root,as_of=self.end,cutoff=cutoff,require_point_in_time=False)
        self.assertEqual(len(result['growth250']['points']),200)
        self.assertEqual(result['growth250']['points'][-1]['date'],str(self.end))
        self.assertFalse(result['growth250']['point_in_time_verified'])
        with self.assertRaisesRegex(MarketRegimeSourceError,'after cutoff'):
            load_index_series(self.root,as_of=self.end,cutoff=cutoff)

    def test_pit_valid_when_originals_observed_before_cutoff(self):
        self.observed='2026-09-30T16:30:00+09:00';self.collect();self.legacy()
        result=load_index_series(self.root,as_of=self.end,cutoff=datetime.fromisoformat('2026-09-30T21:00:00+09:00'))
        self.assertTrue(result['topix']['point_in_time_verified'])
        with self.assertRaises(MarketRegimeSourceError):
            load_index_series(self.root,as_of=self.end,cutoff=datetime.fromisoformat('2026-09-30T08:00:00+09:00'))

    def test_gap_calendar_checksum_and_duplicate_fail(self):
        self.collect();self.legacy();cutoff=datetime.fromisoformat('2026-09-30T21:00:00+09:00')
        path=self.archive.parent/OBSERVATION
        doc=json.loads(path.read_text());ref=doc['ranges']['topix_daily']['pages'][0]
        raw=self.archive/ref['path'];raw.write_text('{}')
        with self.assertRaisesRegex(MarketRegimeSourceError,'checksum'):
            load_index_series(self.root,as_of=self.end,cutoff=cutoff,require_point_in_time=False)

    def test_failed_requests_charge_bootstrap_across_restarts(self):
        calls=[]
        def fail(e,p):calls.append((e,p));raise ingest.ApiError(0)
        for _ in range(3):
            with self.assertRaises(ingest.ApiError):self.collect(fail)
        with self.assertRaises(ingest.RequestBudgetExhausted):self.collect(fail)
        self.assertEqual(len(calls),3)
        d=json.loads((self.archive/index_history.PATH).read_text());self.assertEqual(len(d['attempts']),3)

    def test_pagination_resume_preserves_first_page(self):
        def first(e,p):
            if p.get('pagination_key'):raise ingest.ApiError(0)
            return {'data':self.rows[:100],'pagination_key':'second'}
        with self.assertRaises(ingest.ApiError):self.collect(first)
        def second(e,p):
            self.assertEqual(p['pagination_key'],'second');return {'data':self.rows[100:]}
        d=self.collect(second);self.assertEqual(len(d['pages']),2);self.assertEqual(len(d['attempts']),3)
        self.assertTrue(d['complete'])

    def test_no_authorization_rebinding_or_future_range(self):
        self.collect();self.approval['max_requests']=2
        with self.assertRaises(ingest.IngestionError):self.collect()
        self.approval['run_date']='2026-10-02'
        with self.assertRaises(ingest.IngestionError):self.collect()

    def test_calendar_gap_and_index_gap_are_not_shortened(self):
        self.rows.pop(-5)
        self.collect();self.legacy()
        with self.assertRaisesRegex(MarketRegimeSourceError,'missing an official session'):
            load_index_series(self.root,as_of=self.end,cutoff=datetime.fromisoformat('2026-09-30T21:00:00+09:00'),require_point_in_time=False)
        self.calendar.pop(-4);self.legacy()
        with self.assertRaises(ValueError):
            load_index_series(self.root,as_of=self.end,cutoff=datetime.fromisoformat('2026-09-30T21:00:00+09:00'),require_point_in_time=False)

    def test_duplicate_future_and_invalid_values_are_rejected(self):
        for row in (dict(self.rows[-1]), {'Code':'0070','Date':'2026-10-01','C':10},
                    {'Code':'0000','Date':str(self.end),'C':10},
                    {'Code':'0070','Date':str(self.end),'C':float('nan')}):
            with self.subTest(row=row):
                with self.assertRaises(ingest.IngestionError):
                    index_history.valid_rows(self.rows+[row],start=self.start,through=self.end)

    def test_unknown_or_nontrading_asof_is_rejected(self):
        self.collect();self.legacy()
        for day in (date(2026,9,27), date(2026,10,1)):
            with self.subTest(day=day):
                with self.assertRaises(MarketRegimeSourceError):
                    load_index_series(self.root,as_of=day,cutoff=datetime.combine(day,datetime.min.time()).replace(tzinfo=self.fixture_zone()),require_point_in_time=False)

    @staticmethod
    def fixture_zone():
        return datetime.fromisoformat('2026-09-30T21:00:00+09:00').tzinfo
