import csv
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stock_jp.market_data import jpx
from stock_jp.market_data.jquants import ingest
from stock_jp.operation.liquidity_adapter import apply
from stock_jp.strategy.liquidity import trading_value_window


class PaperLiquidityAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.setup_day('2026-10-07')

    def setup_day(self, day):
        self.day = date.fromisoformat(day)
        self.cutoff = datetime.fromisoformat(str(self.day + timedelta(days=1))+'T04:17:00+09:00')
        self.observed = self.cutoff + timedelta(minutes=1)
        self.run = self.root / 'operations/runs' / day
        self.config = {'jquants': {'enabled': True, 'minimum_liquidity_sessions': 20,
                       'maximum_official_price_age_days': 7}}
        ingest.atomic_json(self.root/'config/policy.json', {'operation_mode':'PAPER','broker_submission':'HUMAN_ONLY'})
        ingest.atomic_json(self.root/'config/sources.json', self.config)
        self.record = {'run_date':day,'data_date':day,'status':'COLLECTED',
                       'config_sha256':ingest.digest((self.root/'config/sources.json').read_bytes()),
                       'requests':[{'reserved_at':self.cutoff.isoformat()}]}
        self.pin_record()
        self.calendar, self.bars = [], []
        for offset in range(-59, 8):
            d = self.day + timedelta(days=offset)
            self.calendar.append({'date':str(d),'holiday_division':'1' if d.weekday()<5 else '0'})
            if offset <= 0 and d.weekday()<5:
                self.bars.append({'Date':str(d),'Code':'12340','O':10,'H':11,'L':9,'C':10,'Vo':100,'Va':1000,'AdjFactor':1})
        self.result = {'providers':{name:{'status':'OK','request_count':1} for name in
                       ['jquants_security_master','jquants_trading_calendar','jquants_market_data',
                        'jquants_financials','jquants_earnings_calendar']},
                       'source_rows':[], 'successful_gap_sources':set(), 'tasks':[],
                       'blocking_failures':[{'source':'nightly_jquants_scan','reason':'incomplete'}]}
        self.result['providers']['jquants_market_data'].update(status='INCOMPLETE',error='missing or invalid bar in fixed liquidity window')
        self.write_sources()

    def pin_record(self):
        p=self.root/f'operations/acquisition/nightly/{self.day}.json'
        ingest.atomic_json(p,self.record)
        self.target={'day':str(self.day),'daily_status':'COLLECTED','cutoff_at_jst':self.cutoff.isoformat(),
                     'adoption_record_sha256':ingest.digest(p.read_bytes())}

    def write_sources(self):
        cal=self.run/'trading-calendar.json'
        ingest.atomic_json(cal,{'rows':self.calendar,'retrieved_at_jst':self.observed.isoformat()})
        bars=self.run/'reference-data/jquants-daily-bars.csv.gz'
        bars.parent.mkdir(parents=True,exist_ok=True)
        jpx._atomic_csv_gzip(bars,jpx.BAR_FIELDS,jpx._normalize_bars(self.bars))
        ingest.atomic_json(self.run/f'raw-sources/jquants-bars-1234-{self.day}.json',{'pages':[{'data':self.bars}]})
        self.manifest={'run_id':str(self.day),'cutoff_at_jst':self.cutoff.isoformat(),
                       'retrieved_at_jst':self.observed.isoformat(), 'datasets':{
                           'trading_calendar':jpx._manifest_entry(cal,self.run,len(self.calendar),'https://api.jquants.com/v2/markets/calendar'),
                           'jquants_daily_bars':jpx._manifest_entry(bars,self.run,len(self.bars),'https://api.jquants.com/v2/equities/bars/daily')}}
        ingest.atomic_json(self.run/'reference-data/manifest.json',self.manifest)

    def call(self):
        with patch('socket.socket',side_effect=AssertionError('network forbidden')):
            result=apply(root=self.root,run_id=str(self.day),target=self.target,cutoff=self.cutoff,
                         observed=self.observed,targets={'1234'},config=self.config,run_dir=self.run,result=self.result)
        manifest=json.loads((self.run/'reference-data/manifest.json').read_text())
        with (self.run/'reference-data/liquidity-20d.csv').open() as f:
            rows=list(csv.DictReader(f))
        return result,manifest['paper_liquidity_adapter'],rows

    def test_delayed_morning_uses_adopted_session_without_changing_formula(self):
        with self.assertRaises(ValueError):
            trading_value_window(self.bars,self.calendar,code='1234',cutoff=self.cutoff.date())
        window,values=trading_value_window(self.bars,self.calendar,code='1234',cutoff=self.day)
        result,audit,rows=self.call()
        self.assertEqual(audit['status'],'ALIGNED')
        self.assertEqual(rows[0]['as_of_date'],str(self.day))
        self.assertEqual(float(rows[0]['average_trading_value_20d']),sum(values)/len(window))
        self.assertEqual(rows[0]['sessions'],'20')
        self.assertFalse(audit['historical_point_in_time_certified'])
        self.assertTrue(audit['source_observed_after_cutoff'])
        self.assertEqual(audit['cutoff_at_jst'],self.cutoff.isoformat())
        self.assertEqual(audit['observed_at_jst'],self.observed.isoformat())
        self.assertEqual(result['source_rows'][0]['retrieved_at_jst'],self.observed.isoformat())
        self.assertEqual(result['blocking_failures'],[])

    def test_month_year_boundary_and_same_night_keep_identical_twenty_sessions(self):
        for day in ['2026-09-30','2026-12-31','2026-10-07']:
            with self.subTest(day=day):
                self.setup_day(day)
                if day=='2026-10-07':
                    self.cutoff=datetime.fromisoformat(day+'T21:30:00+09:00')
                    self.observed=self.cutoff
                    self.record['requests'][0]['reserved_at']=self.cutoff.isoformat()
                    self.pin_record();self.write_sources()
                self.assertEqual(self.call()[1]['status'],'ALIGNED')

    def test_newer_session_close_boundary_cannot_select_an_older_window(self):
        for clock, expected in [('15:29:59','ALIGNED'), ('15:30:00','BLOCKED'), ('15:30:01','BLOCKED')]:
            with self.subTest(clock=clock):
                self.setup_day('2026-10-07')
                self.cutoff=datetime.fromisoformat('2026-10-08T'+clock+'+09:00')
                self.observed=self.cutoff
                self.pin_record();self.write_sources()
                self.assertEqual(self.call()[1]['status'],expected)

    def test_rerun_is_idempotent_and_does_not_rewrite_observation(self):
        first=self.call()
        before=(self.run/'reference-data/manifest.json').read_bytes()
        self.assertEqual(self.call()[1:],first[1:])
        self.assertEqual((self.run/'reference-data/manifest.json').read_bytes(),before)

    def test_future_bar_is_rejected_even_when_it_would_not_change_average(self):
        self.bars.append({**self.bars[-1],'Date':str(self.day+timedelta(days=1))})
        self.write_sources()
        result,audit,rows=self.call()
        self.assertEqual(audit['status'],'BLOCKED');self.assertFalse(rows)
        self.assertIn('future bar',audit['error'])
        self.assertNotIn('official_market_data',result['successful_gap_sources'])

    def test_missing_invalid_latest_no_execution_and_calendar_gaps_stay_blocked(self):
        for failure in ['missing','partial','latest_null','calendar','duplicate','nineteen']:
            with self.subTest(failure=failure):
                self.setup_day('2026-10-07')
                if failure=='missing':self.bars.pop(-2)
                elif failure=='partial':self.bars[-2]['Va']=None
                elif failure=='latest_null':self.bars[-1].update(dict.fromkeys(['O','H','L','C','Vo','Va']))
                elif failure=='calendar':self.calendar=[r for r in self.calendar if r['date']!=str(self.day-timedelta(days=1))]
                elif failure=='duplicate':self.bars.append(self.bars[-1])
                else:self.config['jquants']['minimum_liquidity_sessions']=19
                self.write_sources()
                _,audit,rows=self.call()
                self.assertEqual(audit['status'],'BLOCKED');self.assertFalse(rows)

    def test_explicit_no_execution_still_counts_as_zero_in_same_window(self):
        self.bars[-3].update(dict.fromkeys(['O','H','L','C','Vo','Va']))
        self.write_sources()
        _,audit,rows=self.call()
        self.assertEqual(audit['status'],'ALIGNED')
        self.assertEqual(float(rows[0]['average_trading_value_20d']),950)
        self.assertEqual(float(rows[0]['median_trading_value_20d']),1000)

    def test_holiday_unadopted_future_observation_and_tampered_prices_rejected(self):
        for failure in ['holiday','unadopted','hash','future_adoption','future_observation','future_query','raw_tamper','missing_next_calendar','tamper','live']:
            with self.subTest(failure=failure):
                self.setup_day('2026-10-07')
                if failure=='holiday':
                    for r in self.calendar:
                        if r['date']==str(self.day):r['holiday_division']='0'
                    self.write_sources()
                elif failure=='unadopted':(self.root/f'operations/acquisition/nightly/{self.day}.json').unlink()
                elif failure=='hash':self.target['adoption_record_sha256']='0'*64
                elif failure=='future_adoption':
                    self.record['requests'][0]['reserved_at']=(self.cutoff+timedelta(seconds=1)).isoformat();self.pin_record()
                elif failure=='future_observation':self.observed=self.cutoff-timedelta(seconds=1)
                elif failure=='future_query':
                    self.manifest['jquants_checkpoint']={'pages':[{'retrieved_at_jst':(self.observed+timedelta(seconds=1)).isoformat()}]}
                    ingest.atomic_json(self.run/'reference-data/manifest.json',self.manifest)
                elif failure=='raw_tamper':
                    self.bars[-1]['Va']=2000
                    ingest.atomic_json(self.run/f'raw-sources/jquants-bars-1234-{self.day}.json',{'pages':[{'data':self.bars}]})
                elif failure=='missing_next_calendar':
                    self.calendar=[r for r in self.calendar if r['date']!=self.cutoff.date().isoformat()]
                    self.write_sources()
                elif failure=='tamper':(self.run/'reference-data/jquants-daily-bars.csv.gz').write_bytes(b'bad')
                else:ingest.atomic_json(self.root/'config/policy.json',{'operation_mode':'LIVE','broker_submission':'HUMAN_ONLY'})
                _,audit,rows=self.call();self.assertEqual(audit['status'],'BLOCKED');self.assertFalse(rows)

    def test_other_provider_failures_never_disappear(self):
        self.result['providers']['jquants_financials']['status']='ERROR'
        result,audit,_=self.call()
        self.assertEqual(audit['status'],'ALIGNED')
        self.assertEqual(result['providers']['jquants_financials']['status'],'ERROR')
        self.assertIn('nightly_jquants_scan',[f['source'] for f in result['blocking_failures']])

    def test_transport_failure_cannot_be_repaired_from_leftover_prices(self):
        self.result['providers']['jquants_market_data']['status']='ERROR'
        self.assertEqual(self.call()[1]['status'],'BLOCKED')


class PaperLiquidityWiringTest(unittest.TestCase):
    def test_only_bound_paper_path_aligns_frozen_collector_after_midnight(self):
        from tests.test_nightly_operation import NightlyOperationTest, FIXTURES
        from tests.operation_test_support import write_price_archive
        from stock_jp.operation.nightly import start_nightly_run
        for bound in (False, True):
            with self.subTest(bound=bound):
                fixture=NightlyOperationTest();fixture.setUp()
                try:
                    fixture._add_holding('1234')
                    root=fixture.root
                    write_price_archive(root,['1234'])
                    cutoff='2026-09-01T04:17:00+09:00'
                    record={'status':'COLLECTED','run_date':'2026-08-31','data_date':'2026-08-31',
                            'requests':[{'reserved_at':cutoff}],
                            'config_sha256':ingest.digest((root/'config/sources.json').read_bytes())}
                    path=root/'operations/acquisition/nightly/2026-08-31.json'
                    ingest.atomic_json(path,record)
                    target={'day':'2026-08-31','daily_status':'COLLECTED','cutoff_at_jst':cutoff,
                            'adoption_record_sha256':ingest.digest(path.read_bytes())}
                    with patch('socket.socket',side_effect=AssertionError('network forbidden')):
                        result=start_nightly_run(root=root,at=cutoff,cutoff=cutoff,target_run_id='2026-08-31',
                                                 market_target=target if bound else None,fixture_dir=FIXTURES)
                    run=root/'operations/runs/2026-08-31'
                    manifest=json.loads((run/'reference-data/manifest.json').read_text())
                    health=json.loads((run/'provider-health.json').read_text())
                    if bound:
                        self.assertEqual(manifest['paper_liquidity_adapter']['status'],'ALIGNED')
                        self.assertEqual(health['providers']['jquants_market_data']['status'],'OK')
                        self.assertEqual(manifest['paper_liquidity_adapter']['original_provider']['status'],'INCOMPLETE')
                    else:
                        self.assertNotIn('paper_liquidity_adapter',manifest)
                        self.assertEqual(health['providers']['jquants_market_data']['status'],'INCOMPLETE')
                    self.assertNotEqual(result['status'],'completed')
                finally:
                    fixture.tearDown()
