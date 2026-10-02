from datetime import datetime, date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import jquants_daily as d, jquants_ingest as i, market_data_git as codec


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.private=self.root/'operations/private';self.private.mkdir(parents=True)
        self.config={'jquants':{'enabled':True,'plan':'light','addons':[],'base_url':i.BASE_URL,'api_key_env':'JQUANTS_API_KEY','ingestion':{'enabled':True,'max_requests_per_run':12,'archive_root':'operations/private/market-data/jquants'}},'price_source':{'enabled':True,'provider':'jquants_v2_private_archive','storage':'private_local','archive_root':'operations/private/market-data/daily-prices'}}
        self.save_config();self.archive=self.private/'market-data/jquants';self.calls=[]
        self.at=datetime.fromisoformat('2026-10-02T21:00:00+09:00')

    def save_config(self):
        (self.private/'source-config.json').write_text(json.dumps(self.config))

    def transport(self,endpoint,params):
        self.calls.append((endpoint,dict(params)))
        day=params.get('date',params.get('from',self.at.date().isoformat()))
        if endpoint=='/markets/calendar':rows=[{'Date':day,'HolDiv':'0' if date.fromisoformat(day).weekday()>=5 else '1'}]
        elif endpoint=='/equities/master':rows=[{'Date':day,'Code':'12340','ProdCat':'011','CoName':'Fixture'}]
        elif endpoint=='/equities/bars/daily':rows=[{'Date':day,'Code':'12340','O':100,'H':101,'L':99,'C':100,'Vo':100}]
        elif endpoint in ('/indices/bars/daily/topix','/equities/valuation'):rows=[{'Date':day}]
        elif endpoint=='/fins/summary':rows=[{'DiscDate':day}]
        else:rows=[]
        return {'data':rows}

    def run_daily(self,**kwargs):
        with patch.object(i,'utc_now',return_value=self.at.isoformat()):
            return d.run(self.root,at=self.at,transport=kwargs.pop('transport',self.transport),sleep=lambda _:None,**kwargs)

    def test_daily_empty_duplicate(self):
        r=self.run_daily();self.assertEqual(r['status'],'COLLECTED');self.assertEqual(r['requests_used'],9)
        self.assertEqual(r['readiness'],'NOT_EVALUATED');self.assertEqual(r,self.run_daily());self.assertEqual(len(self.calls),9)
        s=d.read_state(d.run_path(self.archive,self.at.date()))
        self.assertEqual(s['tasks']['investor_types:2026-10-02']['rows'],0)
        self.assertEqual(s['tasks']['financial_summary:2026-10-02']['timing'],'provisional')

    def test_weekend_financial_correction_without_price_day(self):
        self.run_daily();self.calls.clear();self.at=datetime.fromisoformat('2026-10-03T21:00:00+09:00')
        r=self.run_daily();self.assertEqual(r['status'],'HOLIDAY');self.assertIsNone(r['data_date'])
        self.assertEqual([x[0] for x in self.calls],['/fins/summary','/markets/calendar'])
        self.assertEqual(self.calls[0][1],{'date':'2026-10-02'});self.assertEqual(r,self.run_daily());self.assertEqual(len(self.calls),2)
        self.assertEqual(json.loads((self.private/'market-data/daily-prices/latest.json').read_text())['latest_trading_date'],'2026-10-02')

    def test_before_21_wait_resume(self):
        self.at=datetime.fromisoformat('2026-10-02T20:00:00+09:00');self.assertEqual(self.run_daily()['status'],'WAIT_PUBLICATION')
        self.at=datetime.fromisoformat('2026-10-02T21:00:00+09:00');self.assertEqual(self.run_daily()['requests_used'],9)

    def test_auth_terminal(self):
        def denied(*_):raise i.ApiError(403,'authentication_failed')
        r=self.run_daily(transport=denied);self.assertEqual(r['status'],'BLOCKED');self.assertEqual(r['requests_used'],1)
        self.assertEqual(r,self.run_daily());self.assertFalse(self.calls)

    def test_network_retry_charged(self):
        def fail(*_):raise i.ApiError(0)
        self.assertEqual(self.run_daily(transport=fail)['status'],'PAUSED')
        r=self.run_daily();self.assertEqual(r['status'],'COLLECTED');self.assertEqual(r['requests_used'],10)

    def test_429_no_automatic_retry(self):
        def fail(*_):raise i.ApiError(429)
        self.assertEqual(self.run_daily(transport=fail)['requests_used'],1)

    def test_budget_across_twelve_restarts(self):
        def fail(*_):raise i.ApiError(500)
        for used in range(1,13):self.assertEqual(self.run_daily(transport=fail)['requests_used'],used)
        r=self.run_daily();self.assertEqual(r['status'],'BUDGET_EXHAUSTED');self.assertEqual(r['requests_used'],12)
        self.assertFalse(self.calls);self.assertEqual(r,self.run_daily())

    def test_crash_after_reservation(self):
        def crash(*_):raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.run_daily(transport=crash)
        self.assertEqual(self.run_daily()['requests_used'],10)

    def paged(self,endpoint,params):
        if endpoint=='/equities/valuation':
            if params.get('pagination_key'):raise i.ApiError(0)
            p=self.transport(endpoint,params);p['pagination_key']='next';return p
        return self.transport(endpoint,params)

    def test_valid_page_resume(self):
        self.assertEqual(self.run_daily(transport=self.paged)['status'],'PAUSED');self.calls.clear()
        r=self.run_daily();self.assertEqual(r['status'],'COLLECTED');self.assertEqual(r['requests_used'],11)
        self.assertEqual(self.calls[0][1]['pagination_key'],'next')

    def test_checkpoint_corruption_before_network(self):
        self.run_daily(transport=self.paged)
        p=self.archive/'datasets/equities_valuation/2026-10-02/checkpoint.json';c=json.loads(p.read_text());c['next_key']='bad';p.write_text(json.dumps(c))
        count=len(self.calls)
        with self.assertRaises(i.IngestionError):self.run_daily()
        self.assertEqual(len(self.calls),count)

    def test_rollover_explicit_resume(self):
        def fail(*_):raise i.ApiError(0)
        self.run_daily(transport=fail);self.at=datetime.fromisoformat('2026-10-03T21:00:00+09:00')
        with self.assertRaisesRegex(i.IngestionError,'resume prior'):self.run_daily()
        r=self.run_daily(run_date=date(2026,10,2));self.assertEqual(r['run_date'],'2026-10-02');self.assertEqual(r['data_date'],'2026-10-02');self.assertEqual(r['requests_used'],10)

    def test_locks(self):
        with d.workspace_lock(self.private):
            with self.assertRaises(i.IngestionError):self.run_daily()
        with i.archive_lock(self.archive):
            with self.assertRaises(i.IngestionError):self.run_daily()
        self.assertFalse(self.calls)

    def test_corrupt_journal_and_configuration(self):
        self.run_daily();self.config['jquants']['ingestion']['max_requests_per_run']=11;self.save_config()
        with self.assertRaisesRegex(i.IngestionError,'configuration changed'):self.run_daily()
        p=d.run_path(self.archive,self.at.date());s=json.loads(p.read_text());s['budget']=99;p.write_text(json.dumps(s))
        with self.assertRaises(i.IngestionError):self.run_daily()

    def test_restore_reuses_budget_and_completed_partitions(self):
        self.run_daily();store=self.root/'stock.jp/data/jquants-archive';codec.pack(self.private/'market-data',store)
        with tempfile.TemporaryDirectory() as temp:
            import shutil
            root=Path(temp);(root/'operations/private').mkdir(parents=True)
            (root/'operations/private/source-config.json').write_bytes((self.private/'source-config.json').read_bytes())
            shutil.copytree(store,root/'stock.jp/data/jquants-archive')
            r=d.run(root,at=self.at,transport=lambda *_:self.fail('network'))
            self.assertEqual(r['status'],'COLLECTED');self.assertEqual(r['requests_used'],9)

    def test_local_checkpoint_not_overlaid(self):
        self.run_daily();codec.pack(self.private/'market-data',self.root/'stock.jp/data/jquants-archive')
        p=self.archive/'sentinel';p.write_text('keep');self.run_daily();self.assertEqual(p.read_text(),'keep')

    def test_corrupt_object_no_network(self):
        self.run_daily();count=len(self.calls);next((self.archive/'objects').rglob('*.json')).write_text('{}')
        with self.assertRaises(i.IngestionError):self.run_daily()
        self.assertEqual(count,len(self.calls))

    def test_empty_prices_fail_closed(self):
        def empty(e,p):return {'data':[]} if e=='/equities/bars/daily' else self.transport(e,p)
        self.assertEqual(self.run_daily(transport=empty)['status'],'PAUSED')
        self.assertFalse((self.private/'market-data/daily-prices/latest.json').exists())

    def test_future_wrong_contract(self):
        with self.assertRaises(i.IngestionError):self.run_daily(run_date=date(2026,10,3))
        self.config['jquants']['plan']='premium';self.save_config()
        with self.assertRaises(i.IngestionError):self.run_daily()

    def test_late_prices_resume_with_remaining_budget(self):
        def empty(e,p):return {'data':[]} if e=='/equities/bars/daily' else self.transport(e,p)
        self.assertEqual(self.run_daily(transport=empty)['error'],'publication_pending')
        r=self.run_daily();self.assertEqual(r['status'],'COLLECTED');self.assertEqual(r['requests_used'],10)

    def test_429_persisted_cooldown(self):
        def throttle(*_):raise i.ApiError(429)
        first=self.run_daily(transport=throttle)
        self.assertEqual(first,self.run_daily());self.assertFalse(self.calls)
        self.assertIsNotNone(first['retry_not_before'])

    def test_exhausted_run_cannot_be_bypassed_next_day(self):
        self.config['jquants']['ingestion']['max_requests_per_run']=1;self.save_config()
        self.assertEqual(self.run_daily()['status'],'BUDGET_EXHAUSTED')
        self.at=datetime.fromisoformat('2026-10-03T21:00:00+09:00')
        with self.assertRaisesRegex(i.IngestionError,'operator review'):self.run_daily()

    def test_missing_journal_cannot_reset_budget(self):
        self.run_daily();d.run_path(self.archive,self.at.date()).unlink()
        with self.assertRaisesRegex(i.IngestionError,'journal missing'):self.run_daily()

    def test_explicit_date_cannot_create_historical_run(self):
        with self.assertRaisesRegex(i.IngestionError,'existing journal'):
            self.run_daily(run_date=date(2026,10,1))

    def test_financial_revision_preserved_and_later_run_budget_shared(self):
        self.run_daily()
        revisions=self.archive/'datasets/financial_summary/2026-10-02/revisions'
        before={p.name:p.read_bytes() for p in revisions.glob('*.json')}
        self.at=datetime.fromisoformat('2026-10-05T21:00:00+09:00')
        r=self.run_daily();self.assertEqual(r['status'],'COLLECTED');self.assertEqual(r['requests_used'],10)
        self.assertTrue(all((revisions/n).read_bytes()==b for n,b in before.items()))
        self.assertEqual(len(list(revisions.glob('*.json'))),2)

    def test_symlink_in_source_rejected(self):
        self.run_daily();(self.archive/'link').symlink_to(self.private/'source-config.json')
        with self.assertRaises(codec.ArchiveError):self.run_daily()


    def test_codec_publication_roundtrip_preserves_daily_journal(self):
        import subprocess
        from scripts import daily_publication as publication
        self.run_daily()
        def git(*args):
            return subprocess.check_output(['git','-C',str(self.root),*args],stderr=subprocess.DEVNULL,text=True).strip()
        git('init');git('config','user.email','synthetic@example.test');git('config','user.name','Synthetic')
        git('commit','--allow-empty','-m','base');base=git('rev-parse','HEAD')
        store=self.root/'stock.jp/data/jquants-archive'
        codec.pack(self.private/'market-data',store)
        git('add','stock.jp/data');git('commit','-m','daily fixture')
        proof=publication.validate_local(self.root,'market','2026-10-02',base,git('rev-parse','HEAD'))
        self.assertEqual(proof['scope'],'partial_acquisition')
        self.assertFalse(proof['operational_readiness'])
        base=git('rev-parse','HEAD')
        path=d.run_path(self.archive,self.at.date());state=json.loads(path.read_text());state['budget']=99;path.write_text(json.dumps(state))
        codec.pack(self.private/'market-data',store);git('add','stock.jp/data');git('commit','-m','invalid budget')
        with self.assertRaises(i.IngestionError):
            publication.validate_local(self.root,'market','2026-10-02',base,git('rev-parse','HEAD'))

    def test_cli_rejects_stale_or_dirty_checkout(self):
        answers={('rev-parse','--show-toplevel'):str(self.root),('remote','get-url','origin'):'https://github.com/kkyosuke/stock.jp.private.git',('branch','--show-current'):'main',('status','--porcelain'):'',('ls-remote','origin','refs/heads/main'):'a'*40+'\trefs/heads/main',('rev-parse','HEAD'):'a'*40}
        def git(command,**_):return answers[tuple(command[3:])]
        with patch.object(d.subprocess,'check_output',side_effect=git):
            d.checkout_preflight(self.root)
            answers[('rev-parse','HEAD')]='b'*40
            with self.assertRaisesRegex(i.IngestionError,'latest remote main'):d.checkout_preflight(self.root)
            answers[('status','--porcelain')]=' M tracked'
            with self.assertRaisesRegex(i.IngestionError,'clean main'):d.checkout_preflight(self.root)

    def test_holiday_early_run_does_not_skip_later_financial_repair(self):
        self.run_daily();self.calls.clear();self.at=datetime.fromisoformat('2026-10-03T00:05:00+09:00')
        self.assertEqual(self.run_daily()['status'],'WAIT_PUBLICATION');self.assertFalse(self.calls)
        self.at=datetime.fromisoformat('2026-10-03T21:00:00+09:00')
        self.assertEqual(self.run_daily()['status'],'HOLIDAY')
        self.assertEqual(self.calls[0][0],'/fins/summary')

    def test_crash_after_financial_publish_before_checkpoint_unlink(self):
        original_unlink = Path.unlink
        checkpoint = self.archive/'datasets/financial_summary/2026-10-02/checkpoint.json'
        def crash(path, *args, **kwargs):
            if path == checkpoint:
                raise KeyboardInterrupt()
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', crash):
            with self.assertRaises(KeyboardInterrupt):
                self.run_daily()
        self.assertTrue(checkpoint.exists())
        self.assertTrue(json.loads(checkpoint.read_text())['complete'])
        manifest = checkpoint.parent/'latest.json'
        published = manifest.read_bytes()
        revision = checkpoint.parent/'revisions'/ (i.digest(published)+'.json')
        self.assertEqual(revision.read_bytes(), published)
        self.calls.clear()
        resumed = self.run_daily()
        self.assertEqual(resumed['status'], 'COLLECTED')
        self.assertEqual(resumed['requests_used'], 9)
        self.assertFalse(checkpoint.exists())
        self.assertEqual(manifest.read_bytes(), published)
        self.assertNotIn('/fins/summary', [e for e, _ in self.calls])
        self.at = datetime.fromisoformat('2026-10-03T21:00:00+09:00')
        self.calls.clear()
        following = self.run_daily()
        self.assertEqual(following['status'], 'HOLIDAY')
        self.assertEqual(following['requests_used'], 2)
        self.assertEqual(self.calls[0], ('/fins/summary', {'date':'2026-10-02'}))
        self.assertEqual(revision.read_bytes(), published)

    def test_completed_checkpoint_recovery_preserves_mismatch(self):
        self.run_daily()
        folder=self.archive/'datasets/financial_summary/2026-10-02'
        manifest=json.loads((folder/'latest.json').read_text())
        checkpoint={'endpoint':'/fins/summary','query':manifest['query'],
                    'started_at':'2026-10-02T22:00:00+09:00','pages':manifest['pages'],
                    'next_key':None,'seen_keys':[],'complete':True}
        path=folder/'checkpoint.json';path.write_text(json.dumps(checkpoint))
        before=path.read_bytes()
        self.assertFalse(d.recover_published_checkpoint(self.archive,'financial_summary',date(2026,10,2)))
        self.assertEqual(path.read_bytes(),before)

    def test_completed_checkpoint_recovery_refuses_corrupt_published_revision(self):
        self.run_daily()
        folder=self.archive/'datasets/financial_summary/2026-10-02'
        raw=(folder/'latest.json').read_bytes();manifest=json.loads(raw)
        checkpoint={'endpoint':'/fins/summary','query':manifest['query'],'started_at':manifest['started_at'],
                    'pages':manifest['pages'],'next_key':None,'seen_keys':[],'complete':True}
        path=folder/'checkpoint.json';path.write_text(json.dumps(checkpoint))
        (folder/'revisions'/(i.digest(raw)+'.json')).write_text('{}')
        with self.assertRaises(i.IngestionError):
            d.recover_published_checkpoint(self.archive,'financial_summary',date(2026,10,2))
        self.assertTrue(path.exists())

    def changed_financial_revision(self):
        self.run_daily()
        old=json.loads((self.archive/'datasets/financial_summary/2026-10-02/latest.json').read_text())
        old_page=self.archive/old['pages'][0]['path']
        self.at=datetime.fromisoformat('2026-10-03T21:00:00+09:00')
        def revised(endpoint,params):
            payload=self.transport(endpoint,params)
            if endpoint=='/fins/summary':payload['data'][0]['FixtureRevision']=2
            return payload
        self.assertEqual(self.run_daily(transport=revised)['status'],'HOLIDAY')
        new=json.loads((self.archive/'datasets/financial_summary/2026-10-02/latest.json').read_text())
        self.assertNotEqual(new['pages'][0]['path'],old['pages'][0]['path'])
        self.at=datetime.fromisoformat('2026-10-04T21:00:00+09:00')
        return old_page

    def test_corrupt_historical_page_stops_before_network_and_pack(self):
        old_page=self.changed_financial_revision();store=self.root/'codec'
        codec.pack(self.private/'market-data',store);pointer=(store/'current.json').read_bytes()
        old_page.write_text('{}');count=len(self.calls)
        with self.assertRaisesRegex(i.IngestionError,'retained revision'):self.run_daily()
        self.assertEqual(len(self.calls),count)
        with self.assertRaisesRegex(codec.ArchiveError,'retained J-Quants'):codec.pack(self.private/'market-data',store)
        self.assertEqual((store/'current.json').read_bytes(),pointer);self.assertTrue(old_page.exists())

    def test_missing_historical_page_stops_before_network_and_pack(self):
        self.changed_financial_revision().unlink();count=len(self.calls)
        with self.assertRaisesRegex(i.IngestionError,'retained revision'):self.run_daily()
        self.assertEqual(len(self.calls),count)
        with self.assertRaises(codec.ArchiveError):codec.pack(self.private/'market-data',self.root/'codec')

    def test_codec_rejects_semantic_corruption_with_valid_compressed_hashes(self):
        self.changed_financial_revision().write_text('{}');store=self.root/'tampered-store'
        # Build a checksummed adversarial fixture, then invoke real verification.
        with patch.object(codec,'validate_archive',return_value=True):codec.pack(self.private/'market-data',store)
        with self.assertRaisesRegex(codec.ArchiveError,'retained J-Quants'):codec.verify(store)
        target=self.root/'rejected-restore'
        with self.assertRaises(codec.ArchiveError):codec.restore(store,target)
        self.assertFalse(target.exists())

    def test_twenty_runner_days_with_correction_resume_restore(self):
        from datetime import timedelta
        import shutil
        start=self.at;interrupted=False
        for offset in range(20):
            self.at=start+timedelta(days=offset)
            def transport(endpoint,params):
                nonlocal interrupted
                if offset==4 and endpoint=='/fins/summary' and not interrupted:
                    interrupted=True;raise i.ApiError(0)
                payload=self.transport(endpoint,params)
                if endpoint=='/fins/summary':
                    for row in payload['data']:row['FixtureRevision']=offset
                return payload
            result=self.run_daily(transport=transport)
            if result['status']=='PAUSED':result=self.run_daily(transport=transport)
            expected='HOLIDAY' if self.at.weekday()>=5 else 'COLLECTED'
            self.assertEqual(result['status'],expected);self.assertLessEqual(result['requests_used'],12)
            self.assertEqual(result['data_date'],None if expected=='HOLIDAY' else self.at.date().isoformat())
            count=len(self.calls);self.assertEqual(result,self.run_daily());self.assertEqual(count,len(self.calls))
            if offset%5==4:
                source=self.private/'market-data';store=self.root/f'codec-{offset}'
                codec.pack(source,store)
                original={str(p.relative_to(source)):p.read_bytes() for p in source.rglob('*') if p.is_file() and p.name!='.ingestion.lock'}
                shutil.move(source,self.root/f'original-{offset}');codec.restore(store,source)
                self.assertTrue(all((source/p).read_bytes()==b for p,b in original.items()))
                self.assertEqual(result,self.run_daily());self.assertEqual(count,len(self.calls))
        self.assertTrue(interrupted)

    def test_missing_historical_receipt_revision_rejected_by_codec(self):
        self.changed_financial_revision()
        state=d.read_state(d.run_path(self.archive,date(2026,10,2)))
        digest=state['tasks']['financial_summary:2026-10-02']['manifest_sha256']
        (self.archive/'datasets/financial_summary/2026-10-02/revisions'/(digest+'.json')).unlink()
        with self.assertRaises(codec.ArchiveError):codec.pack(self.private/'market-data',self.root/'codec')
