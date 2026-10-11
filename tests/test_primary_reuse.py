"""Synthetic inbox replay only: no real providers, checkpoints or operational state."""
import copy
from datetime import datetime
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stock_jp.automation import nightly as n, paper_checkpoint, paper_sources
from stock_jp.market_data import primary_reuse as reuse, primary_sources as primary
from stock_jp.market_data.jquants import ingest
from stock_jp.operation.paper_offline import review_packet
from tests import test_paper_checkpoint as support

DAY = '2026-10-06'
CUTOFF = '2026-10-07T02:30:01+09:00'
TARGET = {'day': DAY, 'cutoff_at_jst': CUTOFF, 'daily_status': 'HOLIDAY'}


class PrimaryReuseTest(unittest.TestCase):
    def setup_root(self, root):
        repo, context = support.PaperCheckpointTest().root_and_context(root)
        config = {'max_requests': 7, 'allowed_hosts': ['allowed'],
                  'issuers': [{'code': '4052', 'feed_url': 'https://allowed/feed', 'from_at_jst': '2026-09-01T00:00:00+09:00'}],
                  'public_series': [{'id': 'vi', 'title': 'VI', 'config_key': 'vi', 'suffix': '.csv'},
                                    {'id': 'ci', 'title': 'CI', 'config_key': 'ci', 'suffix': '.html'}]}
        ingest.atomic_json(root / primary.CONFIG, config)
        path = root / 'config/sources.json'
        source = json.loads(path.read_text())
        source['market_regime'] = {'vi': 'https://allowed/vi', 'ci': 'https://allowed/ci'}
        ingest.atomic_json(path, source)
        items = [{'type': 'pdf', 'date': '2026/10/05 12:00:00', 'link': f'https://allowed/doc/4052/{i}.pdf',
                  'guid': str(i), 'title': f'Synthetic {i}'} for i in range(4)]
        payload = {'header': [{'customer_code': '4052'}], 'item_count': 4, 'item': items}
        self.raw = {'https://allowed/feed': ('eolparts_announcement_0(' + json.dumps(payload) + ');').encode(),
                    'https://allowed/vi': b'date,close\n', 'https://allowed/ci': b'<html>synthetic</html>'}
        self.raw.update({item['link']: b'%PDF-1.7 synthetic ' + item['guid'].encode() for item in items})
        return repo, context

    def capture(self, root, repo, context, limit):
        calls = []
        def download(url):
            if len(calls) >= limit:
                raise ValueError('shared logical-day provider allowance exhausted')
            calls.append(url)
            return self.raw[url]
        value = primary.collect(root, cutoff=datetime.fromisoformat(CUTOFF), download=download)
        receipt = {'cutoff_at_jst': CUTOFF, 'captured_at_jst': datetime.now(primary.JST).isoformat(timespec="seconds"), 'primary_sources': value}
        raw = ingest.encoded(receipt)
        path = f'research/historical-replay/v04-forward/collection/2026-10-07/{reuse.digest(raw)}.json'
        ingest.atomic_bytes(root / path, raw)
        paper_checkpoint.run(root, context, n.Git(repo), save=True, logical_target=TARGET)
        return value, calls

    def two_attempts(self, root):
        repo, context = self.setup_root(root)
        first, calls1 = self.capture(root, repo, context, 7)
        shutil.rmtree(root / 'research')
        shutil.rmtree(root / 'data')
        second, calls2 = self.capture(root, repo, context, 4)
        self.assertEqual((len(calls1), len(calls2)), (7, 4))  # Original defect reproduced without restored primary evidence.
        self.assertEqual([(s['url'], s['sha256']) for s in first['sources'][:4]],
                         [(s['url'], s['sha256']) for s in second['sources'][:4]])
        shutil.rmtree(root / 'research')
        shutil.rmtree(root / 'data')
        return repo, context, first

    def test_restart_restores_all_snapshots_then_hits_seven_sources_without_a_request(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, context, first = self.two_attempts(root)
            before = copy.deepcopy(repo.refs)
            state = (root / 'operations/state.json').read_bytes()
            result = paper_checkpoint.run(root, context, n.Git(repo), save=False, logical_target=TARGET)
            self.assertEqual(result['primary_receipt_count'], 2)
            self.assertEqual(result['resume_cutoff_at_jst'], CUTOFF)
            self.assertFalse((root / f'operations/runs/{DAY}/handoff.json').exists())
            with patch.object(primary, 'OPEN', side_effect=AssertionError('network forbidden')):
                replay = primary.collect(root, cutoff=datetime.fromisoformat(CUTOFF), logical_day=DAY,
                                         download=lambda _: self.fail('unexpected request'))
            self.assertEqual(replay['cache_hits'], 7)
            self.assertEqual(replay['sources'], first['sources'])
            self.assertEqual(replay['blockers'], [])
            packet = review_packet(root, day=DAY, cutoff=CUTOFF)
            self.assertFalse(packet['completed_run'])
            self.assertFalse(packet['canonical_run_created'])
            self.assertIsNone(packet['review_command'])
            self.assertEqual(len(packet['sources']), 7)
            self.assertEqual(repo.refs, before)
            self.assertEqual((root / 'operations/state.json').read_bytes(), state)

    def test_tampering_or_wrong_scope_fails_before_any_fetch(self):
        for mutation in ('bytes', 'config', 'cutoff', 'day', 'receipt'):
            with self.subTest(mutation=mutation), TemporaryDirectory() as temporary:
                root = Path(temporary)
                repo, context, first = self.two_attempts(root)
                paper_checkpoint.run(root, context, n.Git(repo), save=False, logical_target=TARGET)
                cutoff, day = CUTOFF, DAY
                if mutation == 'bytes':
                    (root / first['sources'][0]['path']).write_bytes(b'changed')
                elif mutation == 'config':
                    with (root / primary.CONFIG).open('a') as stream:
                        stream.write(' ')
                elif mutation == 'cutoff':
                    cutoff = '2026-10-07T02:31:01+09:00'
                elif mutation == 'day':
                    day = '2026-10-07'
                else:
                    bundle = json.loads((root / reuse.BUNDLE).read_text())
                    with (root / bundle['receipts'][0]['path']).open('a') as stream:
                        stream.write(' ')
                with self.assertRaises(ValueError):
                    primary.collect(root, cutoff=datetime.fromisoformat(cutoff), logical_day=day,
                                    download=lambda _: self.fail('unexpected request'))

    def test_unknown_url_publication_or_conflicting_raw_is_rejected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, context = self.setup_root(root)
            first, _ = self.capture(root, repo, context, 7)
            for key, bad in (('url', 'https://allowed/unknown.pdf'), ('published_at_jst', '2026-10-08T00:00:00+09:00'),
                             ('code', '9999'), ('review_status', 'APPROVED')):
                sources = copy.deepcopy(first['sources'])
                sources[1][key] = bad
                with self.subTest(key=key), self.assertRaises(ValueError):
                    reuse.validate_sources(root, sources, cutoff=CUTOFF, read=lambda name: (root / name).read_bytes())
            sources = copy.deepcopy(first['sources'])
            duplicate = dict(sources[1], **primary.preserve(root, b'%PDF-1.7 different', '.pdf'))
            with self.assertRaisesRegex(ValueError, 'conflicting'):
                reuse.validate_sources(root, sources + [duplicate], cutoff=CUTOFF, read=lambda name: (root / name).read_bytes())

    def test_missing_or_modified_inbox_member_fails_before_install(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, context, first = self.two_attempts(root)
            git = n.Git(repo)
            revision = repo.refs[n.PAPER_CHECKPOINT]
            tree = git.tree(revision)
            name = first['sources'][0]['path']
            del tree[n.PAPER_RAW_PREFIX + first['sources'][0]['sha256'] + '/' + name]
            with self.assertRaisesRegex(ValueError, 'member missing'):
                paper_sources.stage(root, git, revision, tree, day=DAY, cutoff=CUTOFF)
            self.assertFalse((root / reuse.BUNDLE).exists())

    def test_pinned_local_git_offline_preparation_never_contacts_a_provider_or_changes_refs(self):
        import subprocess
        from stock_jp.operation import paper_offline
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, context, _ = self.two_attempts(root)
            def git(*args, data=None):
                return subprocess.check_output(['git', '-C', str(root), *args], input=data, stderr=subprocess.DEVNULL).decode().strip()
            git('init')
            git('config', 'user.name', 'Synthetic test')
            git('config', 'user.email', 'synthetic@example.invalid')
            git('remote', 'add', 'origin', 'https://github.com/kkyosuke/stock.jp.private.git')
            reader = n.Git(repo)
            def commit(files):
                git('read-tree', '--empty')
                for name, raw in files.items():
                    blob = git('hash-object', '-w', '--stdin', data=raw)
                    git('update-index', '--add', '--cacheinfo', '100644', blob, name)
                return git('commit-tree', git('write-tree'), '-m', 'Synthetic evidence')
            inbox = commit({name: reader.read(blob) for name, blob in repo.tree(repo.refs[n.PAPER_CHECKPOINT]).items()})
            checkpoint = {'runs': {DAY: {'status': 'HOLIDAY', 'config_sha256': reuse.digest((root / 'config/sources.json').read_bytes()),
                                        'requests': [], 'budget': 12}},
                          'scheduled_targets': {'123': {'date': DAY, 'paper_cutoff_at_jst': CUTOFF}}}
            checkpoint_sha = commit({n.JOURNAL: ingest.encoded(checkpoint)})
            git('update-ref', 'refs/remotes/origin/' + n.PAPER_CHECKPOINT, inbox)
            git('update-ref', 'refs/remotes/origin/' + n.CHECKPOINT, checkpoint_sha)
            before = git('show-ref')
            with patch('socket.create_connection', side_effect=AssertionError('offline network forbidden')):
                result = paper_offline.prepare(root, day=DAY, cutoff=CUTOFF, inbox_commit=inbox, checkpoint_commit=checkpoint_sha)
            self.assertEqual(result['status'], 'OFFLINE_PRIMARY_REVIEW_REQUIRED')
            self.assertEqual(len(result['sources']), 7)
            self.assertFalse((root / 'operations/runs').exists())
            self.assertEqual(git('show-ref'), before)
            self.assertEqual(paper_offline.LocalGit(root).read(git('rev-parse', checkpoint_sha + ':' + n.JOURNAL)), ingest.encoded(checkpoint))
            with self.assertRaisesRegex(ValueError, 'pin differs'):
                paper_offline.prepare(root, day=DAY, cutoff=CUTOFF, inbox_commit=checkpoint_sha, checkpoint_commit=checkpoint_sha)

    def test_saved_canonical_cutoff_cannot_be_overridden_by_a_target(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, context = self.setup_root(root)
            ingest.atomic_json(root / f'operations/runs/{DAY}/source-scan-cutoff.json',
                               {'cutoff_at_jst': '2026-10-07T02:00:00+09:00'})
            self.capture(root, repo, context, 7)
            with self.assertRaisesRegex(n.RemoteStop, 'cutoff differs'):
                paper_checkpoint.run(root, context, n.Git(repo), save=False, logical_target=TARGET)
            self.assertFalse((root / reuse.BUNDLE).exists())

    def test_partial_cache_at_exhausted_budget_keeps_sources_without_refunding_requests(self):
        from stock_jp.automation import provider_budget
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, context = self.setup_root(root)
            first, _ = self.capture(root, repo, context, 4)
            paper_checkpoint.run(root, context, n.Git(repo), save=False, logical_target=TARGET)
            calls = []
            def exhausted(url):
                calls.append(url)
                raise provider_budget.SharedBudgetExhausted('shared logical-day provider allowance exhausted')
            replay = primary.collect(root, cutoff=datetime.fromisoformat(CUTOFF), logical_day=DAY, download=exhausted)
            self.assertEqual(replay['sources'], first['sources'])
            self.assertEqual(replay['cache_hits'], 4)
            self.assertTrue(replay['blockers'])
            self.assertFalse(set(calls) & {s['url'] for s in first['sources']})
            self.assertFalse(replay['full_universe_disclosure_coverage_certified'])


    def test_source_changed_between_validation_and_use_is_not_adopted(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, context, first = self.two_attempts(root)
            paper_checkpoint.run(root, context, n.Git(repo), save=False, logical_target=TARGET)
            original_load = reuse.load
            def changed(*args, **kwargs):
                result = original_load(*args, **kwargs)
                (root / first['sources'][1]['path']).write_bytes(b'%PDF-1.7 modified after validation')
                return result
            with patch.object(reuse, 'load', side_effect=changed):
                result = primary.collect(root, cutoff=datetime.fromisoformat(CUTOFF), logical_day=DAY,
                                         download=lambda _: self.fail('unexpected request'))
            self.assertTrue(result['blockers'])
            self.assertNotIn(first['sources'][1]['url'], [s['url'] for s in result['sources']])
