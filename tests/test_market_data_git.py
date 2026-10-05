from tests.operation_test_support import prepare_write
from stock_jp.workspace import project_root, record_path
from datetime import datetime, timezone, timedelta
import fcntl
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import stock_jp.market_data.storage as m


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.store = self.root / 'store'
        (self.source / 'jquants/datasets/demo').mkdir(parents=True)
        (prepare_write(self.source / 'jquants/datasets/demo/checkpoint.json')).write_text('{"complete":false,"next_key":"synthetic-page-2"}')
        (prepare_write(self.source / 'jquants/empty.json')).write_text('{"data":[]}')
        (self.source / 'daily-prices').mkdir(parents=True)
        (prepare_write(self.source / 'daily-prices/synthetic.csv')).write_text('synthetic,only\n1,2\n')

    def test_roundtrip_checkpoints_empty_and_incremental_dedup(self):
        first = m.pack(self.source, self.store)
        before = {p for p in self.store.rglob('*') if p.is_file() and p.name != 'current.json'}
        self.assertEqual(m.pack(self.source, self.store), first)
        (prepare_write(self.source / 'jquants/empty-copy.json')).write_bytes((self.source / 'jquants/empty.json').read_bytes())
        second = m.pack(self.source, self.store)
        self.assertEqual(len({p for p in self.store.rglob('*') if p.is_file() and p.name != 'current.json'} - before), 1)  # index only
        self.assertNotEqual(first['generation'], second['generation'])
        target = self.root / 'restored'
        self.assertTrue(m.restore(self.store, target)['verified'])
        for p in self.source.rglob('*'):
            if p.is_file() and p.name != '.ingestion.lock':
                self.assertEqual(p.read_bytes(), (target / p.relative_to(self.source)).read_bytes())
        with self.assertRaises(m.ArchiveError):
            m.restore(self.store, target)

    def test_corruption_no_partial_restore(self):
        m.pack(self.source, self.store)
        obj = next((self.store / 'objects').rglob('*.json'))
        prepare_write(obj).write_bytes(b'bad')
        target = self.root / 'restored'
        with self.assertRaises(m.ArchiveError):
            m.restore(self.store, target)
        self.assertFalse(target.exists())

    def test_legacy_gzip_generation_restores_identically_then_writes_plain(self):
        entries = {}
        for path in self.source.rglob('*'):
            if path.is_file():
                raw = path.read_bytes()
                entries[path.relative_to(self.source).as_posix()] = dict(sha256=m.digest(raw),
                    bytes=len(raw), chunks=[m.put(self.store,raw[i:i+m.CHUNK]) for i in range(0,len(raw),m.CHUNK)])
        generation = m.put(self.store,m.encoded(dict(version=1,files=entries)),'generations')
        m.atomic(self.store/'current.json',m.encoded(dict(version=1,generation=generation,
            file_count=len(entries),raw_bytes=sum(e['bytes'] for e in entries.values()))))
        target = self.root/'legacy-restored'
        m.restore(self.store,target)
        plain = self.root/'plain-store'
        result = m.pack(target,plain)
        self.assertEqual(result['version'],2)
        self.assertEqual(list(plain.rglob('*.gz')),[])
        restored = self.root/'plain-restored'
        m.restore(plain,restored)
        for name, entry in entries.items():
            self.assertEqual(m.digest((restored/name).read_bytes()),entry['sha256'])
        self.assertTrue(any(plain.rglob('*.csv')))

    def test_plain_size_limit_blocks_before_pointer_publication(self):
        with patch.object(m,'MAX_SHARD',8):
            with self.assertRaises(m.ArchiveError): m.pack(self.source,self.store)
        self.assertFalse((self.store/'current.json').exists())

    def test_unsafe_paths_and_symlinks(self):
        for name in ('../escape', '/tmp/escape', 'jquants/../escape', 'jquants/.env', 'jquants//test'):
            with self.assertRaises(m.ArchiveError):
                m.safe_name(name)
        (self.source / 'jquants/link').symlink_to(self.root)
        with self.assertRaises(m.ArchiveError):
            m.pack(self.source, self.store)

    def test_secret_and_signed_url_patterns(self):
        for payload in (b'{"api_key":"synthetic"}', b'{"url":"https://example.test/file?X-Amz-Signature=synthetic"}', b'Authorization: Bearer synthetic'):
            with self.assertRaises(m.ArchiveError):
                m.scan(payload)

    def test_lock_and_budget_preserve_previous_generation(self):
        m.pack(self.source, self.store)
        previous = (self.store / 'current.json').read_bytes()
        with (prepare_write(self.source / 'jquants/.ingestion.lock')).open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(m.ArchiveError):
                m.pack(self.source, self.store)
        (prepare_write(self.source / 'jquants/new.json')).write_text('{"synthetic":true}')
        with patch.object(m, 'MAX_STORE', 1):
            with self.assertRaises(m.ArchiveError):
                m.pack(self.source, self.store)
        self.assertEqual(previous, (self.store / 'current.json').read_bytes())
        self.assertTrue(m.verify(self.store)['verified'])

    def test_chunking_bomb_and_shard_limit(self):
        data = b'a' * (m.CHUNK + 9)
        (prepare_write(self.source / 'jquants/large.json')).write_bytes(data)
        m.pack(self.source, self.store)
        with patch.object(m, 'MAX_SHARD', 1):
            with self.assertRaises(m.ArchiveError):
                m.put(self.store, b'unique')
        sha = m.put(self.store, data)
        with self.assertRaises(m.ArchiveError):
            m.unpack(self.store, sha, 10)

    def metadata(self):
        return {'repository_full_name': m.DESTINATION, 'visibility': 'private',
                'owner': {'login': 'kkyosuke'}, 'permissions': {'push': True},
                'checked_at': datetime.now(timezone.utc).isoformat()}

    def test_publication_rejects_public_wrong_stale_and_missing_metadata(self):
        self.assertTrue(m.publication_preflight(self.metadata()))
        for key, value in [('visibility', 'public'), ('repository_full_name', 'other/repo'),
                           ('permissions', {'push': False}),
                           ('checked_at', (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat())]:
            metadata = self.metadata()
            metadata[key] = value
            with self.assertRaises(m.ArchiveError):
                m.publication_preflight(metadata)
        with self.assertRaises(m.ArchiveError):
            m.publication_preflight({})

    def test_ci_excludes_licensed_working_tree(self):
        repo = project_root()
        for path in (repo / '.github/workflows').glob('*.yml'):
            text = path.read_text()
            if 'actions/checkout@' in text and path.name not in {'market-data-manual.yml', 'forward-holdout.yml'}:
                self.assertIn('!/data/market/jquants/', text)
                self.assertNotIn('market_data_git.py restore', text)
            if path.name == 'forward-holdout.yml':
                # This private, read-only observer needs the adopted licensed
                # store, but only its derived observation leaves the checkout.
                self.assertNotIn('!/data/market/jquants/', text)
                self.assertIn('--output .work/forward-holdout/observation.json', text)
                self.assertIn('path: .work/forward-holdout/observation.json', text)
                self.assertNotIn('path: data/', text)
                self.assertNotIn('JQUANTS_API_KEY', text)

    def test_stage_requires_owner_confirmation(self):
        private = self.root / 'operations'
        private.mkdir(parents=True)
        (prepare_write(record_path(private.parent, 'sources.json'))).write_text(json.dumps({'market_data_git': {'repository': m.DESTINATION, 'owner_only_access_confirmed': False}}))
        with self.assertRaises(m.ArchiveError):
            m.stage(self.root, self.metadata())


if __name__ == '__main__':
    unittest.main()

class SingleRepositoryStageTest(unittest.TestCase):
    def test_stage_uses_root_private_input_and_vendored_market_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            private = root / 'operations'
            source = record_path(private.parent, '.work/market-data/jquants')
            source.mkdir(parents=True)
            (prepare_write(source / 'latest.json')).write_text('{"synthetic":true}')
            config = {'market_data_git': {'repository': m.DESTINATION,
                      'store_root': 'data/market/jquants', 'owner_only_access_confirmed': True}}
            (prepare_write(record_path(private.parent, 'sources.json'))).write_text(json.dumps(config))
            metadata = {'repository_full_name': m.DESTINATION, 'visibility': 'private',
                        'owner': {'login': 'kkyosuke'}, 'permissions': {'push': True},
                        'checked_at': datetime.now(timezone.utc).isoformat()}
            m.stage(root, metadata)
            self.assertTrue(m.verify(root / 'data/market/jquants')['verified'])
            self.assertFalse((root / 'data/jquants-archive').exists())
            outside = root / 'outside'
            outside.mkdir()
            (root / 'operations').symlink_to(outside)
            with self.assertRaises(m.ArchiveError):
                m.stage(root, metadata)
            (root / 'operations').unlink()
            config['market_data_git']['repository'] = 'kkyosuke/stock.jp'
            (prepare_write(record_path(private.parent, 'sources.json'))).write_text(json.dumps(config))
            with self.assertRaises(m.ArchiveError):
                m.stage(root, metadata)
