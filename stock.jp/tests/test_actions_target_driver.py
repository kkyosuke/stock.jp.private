"""Offline real driver/runner/codec/Git round trips with synthetic transports."""
import copy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from scripts import actions_target_driver as target, actions_owner as owner, actions_targets as targets
from scripts import actions_daily_remote as r, actions_publication as pub, actions_authorization as auth
from scripts import market_data_git as codec, jquants_daily as daily, jquants_ingest as ingest
from scripts.actions_sharded import ShardedStore
from scripts.git_sandbox import git
import test_actions_driver as fixture
import test_actions_daily_remote as remote_fixture
import test_jquants_history as history_fixture


class GitHub(fixture.LocalGitHub):
    def __init__(self, *args):
        super().__init__(*args)
        self.runs = {}
    def handle(self, method, path, body):
        route = urlsplit(path.removeprefix(pub.PREFIX)).path
        if route.startswith('/actions/runs/') and '/attempts/' in route:
            self.calls.append((method, path, body))
            bits = route.split('/')
            return copy.deepcopy(self.runs[(int(bits[3]), int(bits[5]))])
        result = super().handle(method, path, body)
        if route.startswith('/compare/'):
            base, head = route.rsplit('/', 1)[1].split('...')
            merged = git(self.root, 'merge-base', base, head).decode().strip()
            result['merge_base_commit'] = {'sha':merged}
            if merged != base: result['status'] = 'diverged'
        return result


class TargetDriverTests(unittest.TestCase):
    def setUp(self):
        self.old = fixture.DriverTests()
        self.old.setUp()
        self.addCleanup(self.old.doCleanups)
        self.root, self.source = self.old.root, self.old.source
        self.now = self.old.fixture.at + timedelta(days=1)
        self.remote = GitHub(self.root, self.old.context['sha'])
        with tempfile.TemporaryDirectory() as cache:
            initial = pub.bootstrap_checkpoint(ShardedStore(self.remote,
                authorization=remote_fixture.authorization(), cache=cache), self.source, 'actions-v1')
        receipt = dict(version=1, repository=r.REPO, activation_id='synthetic-target-v1', observed_by='kkyosuke',
            observed_at=(self.now-timedelta(days=1)).isoformat(), scheduler=dict(id='synthetic-task', enabled=False),
            cloud_writers=[dict(id='synthetic-cloud-run', terminated=True, queued_resume=False)],
            evidence=[dict(reference='synthetic-fixture-only', sha256='a'*64)])
        self.receipt = receipt
        self.scope = dict(self.old.scope, version=2, activation_id='synthetic-target-v1',
            assurance='retired-external-cooperative-github', checkpoint_initial_sha=initial,
            retirement_sha256=codec.digest(codec.encoded(receipt)),
            history_campaign=dict(start_date='2026-09-23', as_of='2026-10-02', sessions=8))
        for key in ('old_scheduler_id','old_writer','old_lease_id','handover_sha256','handover_epoch'):
            self.scope.pop(key, None)
        self.context = dict(self.old.context, run_id=100, run_attempt=1)
        self.save_scope()
        self.provider = history_fixture.HistoryTest()
        self.provider.setUp()
        self.addCleanup(self.provider.doCleanups)
        self.calls = self.provider.calls

    def save_scope(self):
        codec.atomic(self.root/owner.RETIREMENT, codec.encoded(self.receipt))
        codec.atomic(self.root/auth.MANIFEST, codec.encoded(self.scope))
        git(self.root, 'add', '.github')
        head = self.old.commit('Synthetic reviewed target activation')
        self.remote.refs['heads/main'] = head
        self.remote.pin = codec.digest(codec.encoded(self.scope))
        self.context['sha'] = head
        self.running()

    def running(self):
        c = self.context
        self.remote.runs[(c['run_id'], c['run_attempt'])] = dict(id=c['run_id'], run_attempt=c['run_attempt'],
            head_sha=c['sha'], head_branch='main', path=owner.WORKFLOW, event='workflow_dispatch',
            repository=dict(full_name=r.REPO), status='in_progress')

    def next_run(self, *, ended=True):
        if ended: self.remote.runs[(self.context['run_id'], self.context['run_attempt'])]['status'] = 'completed'
        head = self.remote.refs['heads/main']
        git(self.root, 'reset', '--hard', head)
        self.context.update(sha=head, run_id=self.context['run_id']+1)
        self.running()

    def execute(self, day='2026-09-23', provider=None):
        with patch.object(ingest, 'utc_now', return_value=self.now.isoformat()):
            return target.Driver(self.root, self.remote, provider or self.provider.transport,
                pin=self.remote.pin, writer='actions-v1', context=self.context,
                clock=lambda:self.now, sleep=lambda _:None).execute(day)

    def document(self):
        with tempfile.TemporaryDirectory() as cache:
            return ShardedStore(self.remote, authorization=remote_fixture.authorization(), cache=cache).read()[1]

    def test_history_dates_share_batches_and_repeat_is_noop(self):
        days = ['2026-09-23','2026-09-24','2026-09-25','2026-09-28','2026-09-29','2026-09-30','2026-10-01','2026-10-02']
        for day in days:
            self.assertEqual(self.execute(day)['status'], 'RESTORED')
            self.next_run()
        self.assertEqual(len(self.calls), 18)
        document = self.document()
        self.assertEqual(list(map(len, document['targets']['batches'])), [12,6])
        self.assertEqual(len(document['runs']), 8)
        self.assertTrue(all(len(v['state']['requests']) <= 12 for v in document['runs'].values()))
        self.assertEqual(self.execute(days[0])['status'], 'ALREADY_RESTORED')
        self.assertEqual(len(self.calls), 18)
        self.assertEqual(self.remote.merge_writes, 8)
        self.assertEqual(document['runs'][days[0]]['state']['started_at'], self.now.isoformat())
        self.assertFalse(any('/history/' in c[1] for c in self.remote.calls))

    def test_out_of_order_calendar_charge_survives_correct_date_recovery(self):
        result = self.execute('2026-09-24')
        self.assertEqual(result['status'], 'WAIT_TARGET_ORDER')
        self.assertEqual(result['next_target_date'], '2026-09-23')
        self.assertEqual(len(self.calls), 1)
        before = self.document()
        waiting = copy.deepcopy(before['runs']['2026-09-24'])
        self.assertEqual(waiting['phase'], 'IDLE')
        self.assertEqual(waiting['state']['status'], 'RUNNING')
        self.assertEqual(len(waiting['state']['requests']), 1)
        self.assertEqual(self.remote.merge_writes, 0)
        self.next_run()
        self.assertEqual(self.execute('2026-09-24')['status'], 'WAIT_TARGET_ORDER')
        self.assertEqual(len(self.calls), 1)
        self.next_run()
        self.assertEqual(self.execute('2026-09-23')['status'], 'RESTORED')
        recovered = self.document()
        self.assertEqual(recovered['runs']['2026-09-24'], waiting)
        self.assertEqual(recovered['targets']['batches'][0][:1], before['targets']['batches'][0])
        self.assertEqual(len(self.calls), 4)
        self.next_run()
        self.assertEqual(self.execute('2026-09-24')['status'], 'RESTORED')
        after = self.document()
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(len(after['runs']['2026-09-24']['state']['requests']), 3)
        self.assertEqual(sum(map(len, after['targets']['batches'])), 6)
        self.assertEqual(after['runs']['2026-09-24']['reservation_ids'][:1], waiting['reservation_ids'])

    def test_pre_fix_out_of_order_journal_can_resume_earliest(self):
        original = targets.TargetCheckpoint.collect_history
        def old_order_failure(checkpoint, *args):
            original(checkpoint, *args)
            if getattr(checkpoint, 'wait_for_target', None):
                raise r.RemoteStop('process missing history dates in order')
        with patch.object(targets.TargetCheckpoint, 'collect_history', old_order_failure):
            with self.assertRaisesRegex(r.RemoteStop, 'missing history dates'): self.execute('2026-09-24')
        before = self.document()['runs']['2026-09-24']
        self.next_run()
        self.assertEqual(self.execute('2026-09-23')['status'], 'RESTORED')
        self.assertEqual(self.document()['runs']['2026-09-24'], before)
        self.assertEqual(len(self.calls), 4)

    def test_out_of_order_failed_calendar_cannot_be_deferred(self):
        def failed(*args): raise ingest.ApiError(503, 'known')
        self.assertEqual(self.execute('2026-09-24', failed)['status'], 'PAUSED')
        before = self.document()['runs']['2026-09-24']
        self.next_run()
        with self.assertRaisesRegex(ingest.IngestionError, 'unfinished run'): self.execute('2026-09-23')
        self.assertEqual(self.document()['runs']['2026-09-24'], before)
        self.assertEqual(self.calls, [])
        self.next_run()
        self.assertEqual(self.execute('2026-09-24')['status'], 'WAIT_TARGET_ORDER')
        self.assertEqual(len(self.document()['runs']['2026-09-24']['state']['requests']), 2)
        self.next_run()
        self.assertEqual(self.execute('2026-09-23')['status'], 'RESTORED')
        self.assertEqual(sum(map(len, self.document()['targets']['batches'])), 5)
        self.assertEqual(len(self.calls), 4)

    def rewrite_main_pointer(self, pointer):
        path = self.root/'stock.jp/data/jquants-archive/current.json'
        path.write_bytes(pointer)
        git(self.root, 'add', 'stock.jp/data/jquants-archive')
        self.remote.refs['heads/main'] = self.old.commit('Synthetic data-only pointer rollback')
        self.next_run()

    def test_current_main_rollback_blocks_noop_and_next_date(self):
        self.assertEqual(self.execute()['status'], 'RESTORED')
        self.next_run()
        pointer = git(self.root, 'show', self.old.base+':stock.jp/data/jquants-archive/current.json')
        self.rewrite_main_pointer(pointer)
        before = self.document()
        count = len(self.calls)
        for selected in ('2026-09-23','2026-09-24'):
            with self.assertRaisesRegex(r.RemoteStop, 'current main lost'): self.execute(selected)
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.document(), before)
        self.assertEqual(self.remote.merge_writes, 1)

    def test_current_main_missing_normalized_price_blocks_noop(self):
        self.assertEqual(self.execute()['status'], 'RESTORED')
        self.next_run()
        archive = self.root/'stock.jp/data/jquants-archive'
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'source'
            codec.restore(archive, source)
            (source/'daily-prices/2026/2026-09-23.csv').unlink()
            codec.pack(source, archive)
        self.rewrite_main_pointer((archive/'current.json').read_bytes())
        with self.assertRaisesRegex(r.RemoteStop, 'current main lost'): self.execute()
        self.assertEqual(len(self.calls), 4)

    def test_publication_resume_rejects_another_open_market_pr(self):
        self.remote.ci_ready = False
        self.assertEqual(self.execute()['status'], 'WAIT_CI')
        previous = self.document()
        other = copy.deepcopy(next(iter(self.remote.prs.values())))
        other.update(id=77, number=77)
        other['head']['ref'] = 'daily/market/2026-09-24'
        self.remote.prs[77] = other
        self.remote.ci_ready = True
        count = len(self.calls)
        writes = sum(c[0] != 'GET' for c in self.remote.calls)
        self.next_run()
        with self.assertRaisesRegex(r.RemoteStop, 'another market'): self.execute()
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.remote.merge_writes, 0)
        self.assertEqual(self.document()['transaction'], previous['transaction'])
        self.assertEqual(sum(c[0] != 'GET' for c in self.remote.calls), writes)

    def test_calendar_invalid_or_missing_dates_never_publish(self):
        def invalid(endpoint, query):
            result = self.provider.transport(endpoint, query)
            if endpoint == '/markets/calendar':
                next(row for row in result['data'] if row['Date'] == '2026-09-26')['Date'] += 'invalid'
            return result
        try:
            result = self.execute(provider=invalid)
        except (ValueError, r.RemoteStop):
            pass
        else:
            self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.remote.merge_writes, 0)

    def test_latest_daily_uses_existing_plan_and_holiday(self):
        self.now = self.old.fixture.at
        self.receipt['observed_at'] = (self.now-timedelta(days=1)).isoformat()
        self.scope['retirement_sha256'] = codec.digest(codec.encoded(self.receipt))
        self.save_scope()
        self.assertEqual(self.execute('2026-10-02', self.old.fixture.transport)['status'], 'RESTORED')
        self.assertEqual(len(self.old.fixture.calls), 9)
        self.next_run()
        self.assertEqual(self.execute('2026-10-02', self.old.fixture.transport)['status'], 'ALREADY_RESTORED')
        self.assertEqual(len(self.old.fixture.calls), 9)
        self.now += timedelta(days=1)
        self.next_run()
        self.assertEqual(self.execute('2026-10-03', self.old.fixture.transport)['status'], 'RESTORED')
        self.assertEqual(self.document()['runs']['2026-10-03']['state']['status'], 'HOLIDAY')

    def test_paced_github_driver_needs_no_external_controller(self):
        raw = self.remote
        start = self.now
        def sleep(seconds): self.now += timedelta(seconds=seconds)
        paced = target.rate.BudgetedTransport(raw, clock=lambda:self.now.timestamp(), sleep=sleep)
        with patch.object(ingest, 'utc_now', side_effect=lambda:self.now.isoformat()):
            result = target.Driver(self.root, paced, self.provider.transport, pin=raw.pin, writer='actions-v1',
                context=self.context, clock=lambda:self.now, sleep=sleep).execute('2026-09-23')
        self.assertEqual(result['status'], 'RESTORED')
        self.assertEqual(len(self.calls), 4)
        self.assertLess((self.now-start).total_seconds(), 5*3600)

    def test_shared_pagination_restored_without_refetch(self):
        def pages(endpoint, query):
            payload = self.provider.transport(endpoint, query)
            if endpoint == '/markets/calendar':
                rows = payload['data']
                return {'data':rows[5:]} if query.get('pagination_key') else {'data':rows[:5], 'pagination_key':'next'}
            return payload
        self.assertEqual(self.execute(provider=pages)['status'], 'RESTORED')
        self.next_run()
        self.assertEqual(self.execute('2026-09-24', pages)['status'], 'RESTORED')
        self.assertEqual(len(self.calls), 7)
        document = self.document()
        self.assertEqual(len(document['targets']['ranges']['trading_calendar']['pages']), 2)
        with tempfile.TemporaryDirectory() as tmp:
            archive, restored = Path(tmp)/'archive', Path(tmp)/'restored'
            pub.download_archive(self.remote, self.remote.refs['heads/main'], archive)
            codec.restore(archive, restored)
            metadata = json.loads((restored/'jquants/target-history/ranges.json').read_bytes())
            self.assertEqual(metadata['ranges'], document['targets']['ranges'])
            self.assertTrue(all(p['retrieved_at'] == self.now.isoformat()
                for value in metadata['ranges'].values() for p in value['pages']))

    def test_future_or_noncanonical_date_has_zero_calls(self):
        for day in ('2026-10-04', '20261002', '2026-1-2'):
            with self.assertRaises((r.RemoteStop, ValueError)): self.execute(day)
        self.assertEqual(self.calls, [])

    def test_missing_retirement_blocks_without_writes(self):
        self.receipt['cloud_writers'][0]['terminated'] = False
        self.scope['retirement_sha256'] = codec.digest(codec.encoded(self.receipt))
        self.save_scope()
        count = len(self.remote.calls)
        with self.assertRaisesRegex(r.RemoteStop, 'retirement'): self.execute()
        self.assertEqual(self.calls, [])
        self.assertTrue(all(c[0] == 'GET' for c in self.remote.calls[count:]))

    def test_checkpoint_deletion_never_bootstraps(self):
        del self.remote.refs[r.REF]
        with self.assertRaises(Exception): self.execute()
        self.assertEqual(self.calls, [])
        self.assertNotIn(r.REF, self.remote.refs)

    def test_owner_must_end_not_expire(self):
        self.remote.ci_ready = False
        self.assertEqual(self.execute()['status'], 'WAIT_CI')
        count = len(self.calls)
        self.next_run(ended=False)
        self.now += timedelta(hours=10)
        with self.assertRaisesRegex(r.RemoteStop, 'termination'): self.execute()
        self.assertEqual(len(self.calls), count)
        self.remote.runs[(100,1)]['status'] = 'completed'
        self.remote.ci_ready = True
        self.assertEqual(self.execute()['status'], 'RESTORED')
        self.assertEqual(len(self.calls), count)

    def test_unknown_provider_outcome_stops_other_date_and_restart(self):
        def unknown(*args): raise ingest.ApiError(0, 'unknown')
        self.execute(provider=unknown)
        self.assertEqual(len(self.document()['runs']['2026-09-23']['state']['requests']), 1)
        self.next_run()
        for day in ('2026-09-23','2026-09-24'):
            with self.assertRaisesRegex(r.RemoteStop, 'unknown outcome'): self.execute(day)
        self.assertEqual(self.calls, [])

    def test_manifest_cannot_reset_consumed_budget(self):
        def failed(*args): raise ingest.ApiError(503, 'known')
        self.execute(provider=failed)
        self.next_run()
        self.scope['expires_at'] = (self.now+timedelta(days=3)).isoformat()
        self.save_scope()
        with self.assertRaisesRegex(r.RemoteStop, 'manifest'): self.execute()
        self.assertEqual(len(self.document()['runs']['2026-09-23']['state']['requests']), 1)

    def test_same_ref_old_closed_pr_is_not_reused(self):
        self.remote.prs[7] = dict(id=7, number=7, state='closed', head=dict(ref='daily/market/2026-09-23'))
        with self.assertRaisesRegex(r.RemoteStop, 'same-date PR'): self.execute()
        self.assertEqual(self.calls, [])

    def test_rejected_campaign_remains_blocked(self):
        self.scope['publication_rejections'] = ['synthetic-rejected']
        self.save_scope()
        with self.assertRaisesRegex(r.RemoteStop, 'rejection'): self.execute()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.scope['publication_rejections'], ['synthetic-rejected'])

    def test_history_holiday_is_verified_without_price_fetch(self):
        self.assertEqual(self.execute('2026-09-26')['status'], 'RESTORED')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.document()['runs']['2026-09-26']['state']['status'], 'HOLIDAY')

    def test_empty_business_price_is_not_success(self):
        def empty(endpoint, query):
            payload = self.provider.transport(endpoint, query)
            return {'data':[]} if endpoint == '/equities/bars/daily' else payload
        self.assertEqual(self.execute(provider=empty)['status'], 'PAUSED')
        self.assertEqual(self.remote.merge_writes, 0)

    def test_historical_master_cannot_use_current_date(self):
        def wrong(endpoint, query):
            payload = self.provider.transport(endpoint, query)
            if endpoint == '/equities/master': payload['data'][0]['Date'] = '2026-10-03'
            return payload
        self.assertEqual(self.execute(provider=wrong)['status'], 'BLOCKED')
        self.assertEqual(self.remote.merge_writes, 0)

    def test_owner_audit_is_bound_and_immutable(self):
        self.assertEqual(self.execute()['status'], 'RESTORED')
        document = self.document()
        broken = copy.deepcopy(document)
        broken['run_owner']['run_attempt'] += 1
        with self.assertRaisesRegex(r.RemoteStop, 'transfer chain'): r.validate(broken)
        broken = copy.deepcopy(document)
        broken['owner_history'][0]['observed_at'] = '2026-10-03'
        with self.assertRaisesRegex(r.RemoteStop, 'timestamp'): r.validate(broken)
        broken = copy.deepcopy(document)
        broken['owner_history'][0]['observed_at'] = (self.now+timedelta(seconds=1)).isoformat()
        broken['fence'] = 'a'*32
        with self.assertRaises(r.RemoteStop): r.check_transition(document, broken)

    def test_current_owner_attempt_mismatch_is_rejected(self):
        self.remote.runs[(100,1)]['run_attempt'] = 2
        with self.assertRaisesRegex(r.RemoteStop, 'identity mismatch'): self.execute()
        self.assertEqual(self.calls, [])

    def test_owner_conflict_after_reservation_never_calls_provider(self):
        original = owner.observe
        def conflict(checkpoint, current):
            if checkpoint.document['runs'].get('2026-09-23', {}).get('phase') == 'IN_FLIGHT':
                raise r.RemoteStop('checkpoint owner or revision changed')
            return original(checkpoint, current)
        with patch.object(owner, 'observe', side_effect=conflict):
            with self.assertRaisesRegex(r.RemoteStop, 'owner'): self.execute()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.document()['runs']['2026-09-23']['phase'], 'IN_FLIGHT')
        self.assertEqual(len(self.document()['targets']['batches'][0]), 1)

    def test_midnight_after_reservation_keeps_charge_without_send(self):
        original = targets.TargetCheckpoint.reserve
        def crossing(checkpoint, state):
            original(checkpoint, state)
            self.now += timedelta(days=1)
        with patch.object(targets.TargetCheckpoint, 'reserve', crossing):
            with self.assertRaisesRegex(r.RemoteStop, 'day changed'): self.execute()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.document()['runs']['2026-09-23']['phase'], 'IN_FLIGHT')

    def test_pending_publication_intent_is_not_erased_on_restart(self):
        self.remote.loss = lambda method, path: method == 'POST' and path.endswith('/pulls')
        with self.assertRaises(r.RemoteStop): self.execute()
        previous = self.document()['publication_intent']
        count = len(self.calls)
        self.next_run()
        with self.assertRaisesRegex(r.RemoteStop, 'publication intent'): self.execute()
        self.assertEqual(self.document()['publication_intent'], previous)
        self.assertEqual(len(self.calls), count)

    def test_twelfth_request_survives_restarts(self):
        def failed(*args): raise ingest.ApiError(503, 'known')
        for used in range(1,13):
            self.execute(provider=failed)
            self.assertEqual(len(self.document()['runs']['2026-09-23']['state']['requests']), used)
            self.next_run()
        self.assertEqual(self.execute(provider=failed)['status'], 'BUDGET_EXHAUSTED')
        self.assertEqual(len(self.document()['targets']['batches']), 1)


class CalendarValidationTests(unittest.TestCase):
    def test_archive_rejects_noncanonical_duplicate_and_missing_calendar_days(self):
        from scripts import jquants_archive_validation as validation
        for dates in (['2026-09-25','2026-09-26'], ['2026-09-25','2026-09-26invalid'],
                      ['2026-09-25','2026-09-25'], ['2026-09-25'],
                      ['2026-09-25','20260926']):
            with self.subTest(dates=dates):
                payload = codec.encoded({'data':[{'Date':d, 'HolDiv':'1' if d == '2026-09-25' else '0'} for d in dates]})
                digest = codec.digest(payload)
                name = 'jquants/objects/'+digest[:2]+'/'+digest+'.json'
                page = dict(path=name.removeprefix('jquants/'), sha256=digest, bytes=len(payload),
                            retrieved_at='2026-10-03T12:00:00+00:00')
                metadata = dict(campaign=dict(start_date='2026-09-25', as_of='2026-09-26', sessions=1),
                    knowledge='retrieved_now_not_point_in_time', ranges=dict(trading_calendar=dict(
                        query={'from':'2026-09-25','to':'2026-09-26'}, pages=[page],
                        next_key=None, seen_keys=[], complete=True)))
                files = {name:payload, 'jquants/target-history/ranges.json':codec.encoded(metadata)}
                if dates == ['2026-09-25','2026-09-26']:
                    self.assertTrue(validation.validate_archive(files, files.__getitem__))
                else:
                    with self.assertRaises(ValueError): validation.validate_archive(files, files.__getitem__)


class BudgetTests(unittest.TestCase):
    def test_campaign_600_and_50_boundary_and_rewrite(self):
        # Fifty dates each have their own twelve charged reservations. Build the
        # same durable shape the integration runner commits, without 600 HTTPs.
        document = dict(runs={}, targets=dict(campaign=dict(start_date='2026-01-01', as_of='2026-03-01', sessions=40),
                         profiles={}, batches=[], ranges={}))
        for n in range(50):
            day = (date(2026,1,1)+timedelta(days=n)).isoformat()
            ids = [f'{n*12+i:032x}' for i in range(12)]
            document['runs'][day] = dict(reservation_ids=ids)
            document['targets']['profiles'][day] = 'history'
            document['targets']['batches'].append([dict(target_date=day, reservation_id=i) for i in ids])
        targets.validate(document)
        checkpoint = targets.TargetCheckpoint(None, writer='actions-v1', target_date=day, profile='history')
        checkpoint.document = document
        # The reservation boundary itself rejects request 601 before any store IO.
        checkpoint.day = day
        state = dict(run_date=day, requests=[{}], budget=12, config_sha256='a'*64)
        document['runs'][day].update(phase='IDLE', retry_not_before=None,
                                   state=dict(requests=[], budget=12, config_sha256='a'*64))
        with self.assertRaisesRegex(r.RemoteStop, '600'): checkpoint.reserve(state)
        extra = copy.deepcopy(document)
        extra['targets']['batches'].append([])
        with self.assertRaisesRegex(r.RemoteStop, '50 batch'): targets.validate(extra)
        shorter = copy.deepcopy(document['targets'])
        shorter['batches'] = [[]]
        with self.assertRaisesRegex(r.RemoteStop, 'budget reset'): targets.transition(document['targets'], shorter)
