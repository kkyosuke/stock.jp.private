"""No-network adapter integration with actual daily runner/codec, synthetic data."""
import base64
import copy
from datetime import timedelta
import hashlib
import json
import shutil
import unittest
from unittest.mock import patch
from scripts import actions_daily_remote as r, jquants_ingest as i
import test_jquants_daily as fixture


def authorization():
    return dict(enabled=True, publication_authorized=True, checkpoint_authorized=True,
                old_scheduler_stopped=True, main_restored_and_verified=True, exclusive_writer=True,
                publication_rejected=False, unresolved_history=False, open_daily_pr=False,
                repository=r.REPO, visibility='private', mode='PAPER', submission='HUMAN_ONLY', plan='light')


class GitMock:
    def __init__(self):
        self.calls, self.objects = [], {}
        self.fail_patch = self.lose_ack = self.race = self.fail_blob = False
        self.document = dict(version=2, writer='actions-v1', fence='0' * 32,
                             blocked_campaigns=[], runs={}, snapshot=None)
        self.head = self.seed(self.document)

    def put(self, obj):
        ident = hashlib.sha1(r.codec.encoded(obj)).hexdigest()
        self.objects[ident] = copy.deepcopy(obj)
        return ident

    def seed(self, document):
        raw = r.codec.encoded(document)
        blob = r.git_blob(raw)
        self.objects[blob] = dict(encoding='base64', content=base64.b64encode(raw).decode(), size=len(raw))
        tree = self.put(dict(tree=[dict(path='checkpoint.json', mode='100644', type='blob', sha=blob)], truncated=False))
        return self.put(dict(tree={'sha':tree}, parents=[]))

    def __call__(self, method, path, body):
        self.calls.append((method, path, copy.deepcopy(body)))
        path = path.removeprefix('/repos/' + r.REPO)
        if method == 'GET':
            if path == '':
                return dict(full_name=r.REPO, private=True)
            if path == '/git/ref/' + r.REF:
                return {'object': {'sha':self.head}}
            ident = path.rsplit('/', 1)[1]
            return {**copy.deepcopy(self.objects[ident]), 'sha':ident}
        if path == '/git/blobs':
            if self.fail_blob:
                raise OSError('synthetic failure')
            raw = base64.b64decode(body['content'])
            ident = r.git_blob(raw)
            self.objects[ident] = {**body, 'size':len(raw)}
            return {'sha':ident}
        if path == '/git/trees':
            return {'sha':self.put({**body, 'truncated':False})}
        if path == '/git/commits':
            return {'sha':self.put({**body, 'tree':{'sha':body['tree']}})}
        if method == 'PATCH':
            if self.race:
                self.head = self.put(dict(tree=self.objects[self.head]['tree'], parents=[self.head], race=True))
            # GitHub accepts an idempotent PATCH to the current target SHA.
            if self.fail_patch or (body['sha'] != self.head and self.objects[body['sha']]['parents'] != [self.head]):
                raise OSError('non-fast-forward')
            assert body['force'] is False
            self.head = body['sha']
            if self.lose_ack:
                raise OSError('ack lost after write')
            return {'object': {'sha':self.head}}
        raise AssertionError('unreviewed REST operation')


class RemoteDailyTests(unittest.TestCase):
    save_config = fixture.DailyTests.save_config
    transport = fixture.DailyTests.transport
    run_daily = fixture.DailyTests.run_daily

    def setUp(self):
        fixture.DailyTests.setUp(self)
        self.git = GitMock()
        self.store = r.GitCheckpointStore(self.git, authorization=authorization())

    def adapter(self):
        return r.RunnerCheckpoint(self.store, writer='actions-v1', authorization=authorization(), clock=lambda:self.at)

    def remote_run(self, **kwargs):
        return self.run_daily(remote=self.adapter(), **kwargs)

    def fresh_disk(self):
        shutil.rmtree(self.private / 'market-data')

    def ledger(self):
        return self.store.read()[1]['runs'][self.at.date().isoformat()]

    def test_normal_and_fresh_runner_same_day_dedup(self):
        report = self.remote_run()
        self.assertEqual(report['status'], 'COLLECTED')
        self.assertEqual(report['requests_used'], 9)
        self.fresh_disk()
        self.assertEqual(self.remote_run(), report)
        self.assertEqual(len(self.calls), 9)
        self.assertEqual(self.ledger()['phase'], 'COLLECTED')

    def test_twelve_failures_across_ephemeral_runners(self):
        def fail(*_):
            raise i.ApiError(500)
        for used in range(1, 13):
            self.assertEqual(self.remote_run(transport=fail)['requests_used'], used)
            self.fresh_disk()
        self.assertEqual(self.remote_run()['status'], 'BUDGET_EXHAUSTED')
        self.assertEqual(len(self.ledger()['state']['requests']), 12)
        self.assertFalse(self.calls)

    def test_crash_after_reservation(self):
        def crash(*_):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.remote_run(transport=crash)
        self.fresh_disk()
        with self.assertRaisesRegex(r.RemoteStop, 'ambiguous'):
            self.remote_run()
        self.assertEqual(len(self.ledger()['state']['requests']), 1)

    def test_unknown_network_result_cannot_retry(self):
        def fail(*_):
            raise i.ApiError(0)
        self.remote_run(transport=fail)
        self.fresh_disk()
        with self.assertRaisesRegex(r.RemoteStop, 'ambiguous'):
            self.remote_run()
        self.assertEqual(len(self.ledger()['state']['requests']), 1)

    def test_known_429_cooldown_and_retry(self):
        def fail(*_):
            raise i.ApiError(429)
        with patch('scripts.jquants_daily.datetime') as clock:
            from datetime import datetime
            clock.now.side_effect = lambda *_: self.at
            clock.combine.side_effect = datetime.combine
            clock.fromisoformat.side_effect = datetime.fromisoformat
            self.remote_run(transport=fail)
            self.assertEqual(self.ledger()['phase'], 'PAUSED')
            self.fresh_disk()
            self.assertEqual(self.remote_run()['requests_used'], 1)
            self.assertFalse(self.calls)
            self.at += timedelta(seconds=121)
            self.assertEqual(self.remote_run()['requests_used'], 10)

    def test_permission_denial_no_retry(self):
        def fail(*_):
            raise i.ApiError(403)
        self.assertEqual(self.remote_run(transport=fail)['status'], 'BLOCKED')
        self.fresh_disk()
        self.assertEqual(self.remote_run()['requests_used'], 1)
        self.assertFalse(self.calls)

    def test_response_saved_locally_but_cas_fails(self):
        def received(endpoint, params):
            payload = self.transport(endpoint, params)
            self.git.fail_patch = True
            return payload
        with self.assertRaises(r.RemoteStop):
            self.remote_run(transport=received)
        self.git.fail_patch = False
        self.fresh_disk()
        with self.assertRaisesRegex(r.RemoteStop, 'ambiguous'):
            self.remote_run()
        self.assertEqual(len(self.calls), 1)

    def test_reserved_unsent_ack_loss(self):
        original = GitMock.__call__
        def lost(git, method, path, body):
            if method == 'PATCH':
                commit = git.objects[body['sha']]
                tree = git.objects[commit['tree']['sha']]
                blob = git.objects[tree['tree'][0]['sha']]
                doc = json.loads(base64.b64decode(blob['content']))
                if any(run['phase'] == 'IN_FLIGHT' for run in doc['runs'].values()):
                    git.lose_ack = True
            return original(git, method, path, body)
        with patch.object(GitMock, '__call__', lost):
            with self.assertRaises(r.RemoteStop):
                self.remote_run()
        self.git.lose_ack = False
        self.assertFalse(self.calls)
        self.fresh_disk()
        with self.assertRaisesRegex(r.RemoteStop, 'ambiguous'):
            self.remote_run()

    def test_competing_writer_cas_rejected_before_api(self):
        self.git.race = True
        with self.assertRaises(r.RemoteStop):
            self.remote_run()
        self.assertFalse(self.calls)
        self.assertTrue(all(body['force'] is False for method, _, body in self.git.calls if method == 'PATCH'))

    def test_upload_failure_before_reservation(self):
        self.git.fail_blob = True
        with self.assertRaises(r.RemoteStop):
            self.remote_run()
        self.assertFalse(self.calls)

    def test_disabled_and_blocked_campaign_no_upload(self):
        disabled = r.RunnerCheckpoint(self.store, writer='actions-v1')
        with self.assertRaises(r.RemoteStop):
            self.run_daily(remote=disabled)
        self.assertFalse(self.git.calls)
        self.git.document['blocked_campaigns'] = ['unresolved-synthetic-campaign']
        self.git.head = self.git.seed(self.git.document)
        with self.assertRaisesRegex(r.RemoteStop, 'blocked campaign'):
            self.remote_run()
        self.assertFalse(any(method != 'GET' for method, _, _ in self.git.calls))
        self.assertFalse(self.calls)

    def test_history_payload_rejected_before_git(self):
        (self.archive / 'history').mkdir(parents=True)
        (self.archive / 'history/state.json').write_text('{}')
        with self.assertRaisesRegex(r.RemoteStop, 'history payload forbidden'):
            self.remote_run()
        self.assertFalse(self.git.calls)
        self.assertFalse(self.calls)

    def test_page_checkpoint_resume_without_repeat(self):
        def pages(endpoint, params):
            result = self.transport(endpoint, params)
            if endpoint == '/equities/valuation':
                if params.get('pagination_key'):
                    raise i.ApiError(500)
                result['pagination_key'] = 'next'
            return result
        self.assertEqual(self.remote_run(transport=pages)['status'], 'PAUSED')
        self.fresh_disk()
        self.calls.clear()
        self.assertEqual(self.remote_run()['status'], 'COLLECTED')
        self.assertEqual(self.calls[0][1]['pagination_key'], 'next')

    def test_response_before_local_checkpoint_crash_stays_charged(self):
        original = i.atomic_json
        def crash(path, value):
            if path.name == 'checkpoint.json':
                raise KeyboardInterrupt()
            return original(path, value)
        with patch.object(i, 'atomic_json', crash):
            with self.assertRaises(KeyboardInterrupt):
                self.remote_run()
        self.fresh_disk()
        with self.assertRaisesRegex(r.RemoteStop, 'ambiguous'):
            self.remote_run()
        self.assertEqual(len(self.calls), 1)

    def test_crash_after_remote_page_ack_resumes_without_repeating_page(self):
        original = r.RunnerCheckpoint.page_saved
        def crash(adapter, state):
            original(adapter, state)
            raise KeyboardInterrupt()
        with patch.object(r.RunnerCheckpoint, 'page_saved', crash):
            with self.assertRaises(KeyboardInterrupt):
                self.remote_run()
        self.fresh_disk()
        self.assertEqual(self.remote_run()['status'], 'COLLECTED')
        self.assertEqual(len(self.calls), 9)

    def test_old_parent_and_ledger_reset_rejected(self):
        revision, value = self.store.read()
        self.remote_run()
        with self.assertRaisesRegex(r.RemoteStop, 'stale'):
            self.store.cas(revision, value)
        revision, value = self.store.read()
        value['runs'] = {}
        before = len(self.git.calls)
        with self.assertRaisesRegex(r.RemoteStop, 'deletion'):
            self.store.cas(revision, value)
        self.assertFalse(any(method != 'GET' for method, _, _ in self.git.calls[before:]))

    def test_rejected_publication_blocks_even_reads(self):
        facts = {**authorization(), 'publication_rejected': True}
        adapter = r.RunnerCheckpoint(self.store, writer='actions-v1', authorization=facts)
        with self.assertRaisesRegex(r.RemoteStop, 'publication_rejected'):
            self.run_daily(remote=adapter)
        self.assertFalse(self.git.calls)

    def test_next_day_blocks_until_independent_adoption_restore(self):
        self.remote_run()
        self.fresh_disk()
        self.at += timedelta(days=1)
        with self.assertRaisesRegex(r.RemoteStop, 'not adopted'):
            self.remote_run()
        self.assertEqual(len(self.calls), 9)

    def test_git_mock_accepts_same_target_update(self):
        result = self.git('PATCH', '/repos/' + r.REPO + '/git/refs/' + r.REF,
                          {'sha': self.git.head, 'force': False})
        self.assertEqual(result['object']['sha'], self.git.head)

    def test_identical_reservation_bodies_have_unique_commits_and_single_ack(self):
        # The server clock/author/tree/parent are identical. Interleave caller B
        # at caller A's PATCH, after both could have observed the same parent.
        # A broken fixed-message implementation generates one SHA and both ACK.
        self.remote_run()
        expected, document = self.store.read()
        document['fence'] = 'f' * 32
        run = document['runs'][self.at.date().isoformat()]
        receipt = copy.deepcopy(run['state']['requests'][-1])
        run['state']['requests'].append(receipt)
        run['reservation_ids'].append('e' * 32)
        run['phase'] = 'IN_FLIGHT'
        winners, commits = [], []
        original = GitMock.__call__
        interleaved = False

        def concurrent(git, method, path, body):
            nonlocal interleaved
            if method == 'PATCH' and not interleaved:
                interleaved = True
                # Same requested document, including reservation ID and fence.
                self.store.cas(expected, copy.deepcopy(document))
                winners.append('B may send')
            result = original(git, method, path, body)
            if path.endswith('/git/commits') and method == 'POST':
                commits.append(result['sha'])
            return result

        with patch.object(GitMock, '__call__', concurrent):
            with self.assertRaises(r.RemoteStop):
                self.store.cas(expected, copy.deepcopy(document))
                winners.append('A may send')
        self.assertEqual(winners, ['B may send'])
        self.assertEqual(len(commits), 2)
        self.assertEqual(len(set(commits)), 2)
        self.assertEqual(len(self.ledger()['state']['requests']), 10)
        self.assertEqual(self.ledger()['reservation_ids'][-1], 'e' * 32)

    def test_reservation_id_rewrite_rejected(self):
        self.remote_run()
        revision, document = self.store.read()
        document['fence'] = 'f' * 32
        document['runs'][self.at.date().isoformat()]['reservation_ids'][0] = 'e' * 32
        with self.assertRaisesRegex(r.RemoteStop, 'identity rewrite'):
            self.store.cas(revision, document)
