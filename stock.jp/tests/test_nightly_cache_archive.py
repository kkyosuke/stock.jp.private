"""Synthetic-only archive tests for resumable nightly query provenance."""
import copy
import fcntl
import subprocess
import json
from pathlib import Path
import tempfile
import unittest

from scripts import market_data_git as codec
from scripts.jquants_ingest import digest, encoded
from scripts.nightly_jquants_cache import NightlyClient


class NightlyCacheArchiveTests(unittest.TestCase):
    cutoff = '2026-10-02T21:00:00+09:00'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.private = self.root / 'private'
        self.source = self.private / 'market-data'
        self.store = self.root / 'store'
        self.client = NightlyClient(
            private=self.private, cutoff=self.cutoff, retrieved=self.cutoff,
            plan='light', transport=lambda *_: {'data': [{'synthetic': True}]},
            sleep=lambda _: None)
        self.client.get('/equities/master', {'date': '2026-10-02'})
        self.page = next(self.client.root.glob('pages/*.json'))
        self.receipt = json.loads(self.page.read_bytes())
        self.obj = self.client.root / self.receipt['object']['path']

    def write_receipt(self, receipt, rekey=False):
        path = self.page
        if rekey:
            path = path.with_name(digest(encoded(receipt['identity'])) + '.json')
            self.page.unlink()
        path.write_bytes(encoded(receipt))
        return path

    def forge_store(self):
        """Bypass pack validation, keeping transport hashes fully consistent."""
        files = {}
        for path in self.source.rglob('*'):
            if not path.is_file() or path.name == '.ingestion.lock':
                continue
            data = path.read_bytes()
            files[path.relative_to(self.source).as_posix()] = {
                'sha256': digest(data), 'bytes': len(data),
                'chunks': [codec.put(self.store, data)] if data else []}
        generation = codec.put(self.store, codec.encoded({'version': 1, 'files': files}), 'generations')
        codec.atomic(self.store / 'current.json', codec.encoded({
            'version': 1, 'generation': generation, 'file_count': len(files),
            'raw_bytes': sum(entry['bytes'] for entry in files.values())}))

    def test_old_archive_then_cache_roundtrip_and_offline_replay(self):
        legacy = self.root / 'legacy'
        (legacy / 'jquants').mkdir(parents=True)
        (legacy / 'jquants/checkpoint.json').write_text('{"complete":false}')
        (legacy / 'daily-prices').mkdir()
        (legacy / 'daily-prices/synthetic.csv').write_text('synthetic\n')
        codec.pack(legacy, self.store)
        old = self.root / 'old-restored'
        self.assertTrue(codec.restore(self.store, old)['verified'])
        (self.source / 'jquants').mkdir(exist_ok=True)
        (self.source / 'jquants/checkpoint.json').write_bytes((old / 'jquants/checkpoint.json').read_bytes())
        # Two queries share one immutable payload; preserve both receipts.
        self.client.get('/fins/summary', {'date': '2026-10-02'})
        self.assertEqual(len(list(self.client.root.glob('objects/*/*.json'))), 1)
        first = codec.pack(self.source, self.store)
        objects = set(self.store.rglob('*.gz'))
        self.assertEqual(codec.pack(self.source, self.store), first)
        self.assertEqual(set(self.store.rglob('*.gz')), objects)
        target = self.root / 'restored-private/market-data'
        self.assertTrue(codec.restore(self.store, target)['verified'])
        for path in self.source.rglob('*'):
            if path.is_file() and path.name != '.ingestion.lock':
                self.assertEqual(path.read_bytes(), (target / path.relative_to(self.source)).read_bytes())
        replay = NightlyClient(private=target.parent, cutoff=self.cutoff, retrieved=self.cutoff,
                               plan='light', transport=lambda *_: self.fail('unexpected provider call'),
                               sleep=lambda _: None)
        for endpoint in ('/equities/master', '/fins/summary'):
            self.assertEqual(replay.get(endpoint, {'date': '2026-10-02'}), {'data': [{'synthetic': True}]})
        self.assertEqual((replay.requests, replay.cache_hits), (0, 2))

    def test_held_scope_lock_preserves_published_generation(self):
        codec.pack(self.source, self.store)
        before = (self.store / 'current.json').read_bytes()
        with (self.client.root / '.ingestion.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(codec.ArchiveError):
                codec.pack(self.source, self.store)
        self.assertEqual((self.store / 'current.json').read_bytes(), before)
        self.assertTrue(codec.verify(self.store)['verified'])

    def test_duplicate_receipt_and_nested_payload_keys_fail_closed(self):
        # Ordinary json.loads would silently retain the second timestamp.
        raw = encoded(self.receipt)
        duplicate = b'{"retrieved_at_jst":"ignored",' + raw[1:]
        self.page.write_bytes(duplicate)
        with self.assertRaises(codec.ArchiveError):
            codec.pack(self.source, self.store)
        self.write_receipt(self.receipt)
        self.obj.unlink()
        data = b'{"data":[{"synthetic":false,"synthetic":true}]}\n'
        sha = digest(data)
        invalid = self.client.root / 'objects' / sha[:2] / (sha + '.json')
        invalid.parent.mkdir(exist_ok=True)
        invalid.write_bytes(data)
        receipt = copy.deepcopy(self.receipt)
        receipt['object'] = {'path': invalid.relative_to(self.client.root).as_posix(),
                             'sha256': sha, 'bytes': len(data)}
        self.write_receipt(receipt)
        with self.assertRaises(codec.ArchiveError):
            codec.pack(self.source, self.store)
        self.forge_store()
        with self.assertRaises(codec.ArchiveError):
            codec.restore(self.store, self.root / 'rejected')
        self.assertFalse((self.root / 'rejected').exists())

    def test_semantic_publication_validator_accepts_synthetic_cache(self):
        from scripts import daily_publication
        names = ('equities_master', 'equities_daily', 'equities_valuation', 'financial_summary',
                 'earnings_dates', 'earnings_calendar', 'trading_calendar', 'investor_types', 'topix_daily')
        report = {'provider': 'J-Quants API V2', 'declared_plan': 'light', 'declared_addons': [],
                  'all_entitled_complete': False,
                  'datasets': {name: {'status': 'supported', 'collection_status': 'pending_resume'}
                               for name in names}}
        (self.source / 'jquants').mkdir(exist_ok=True)
        (self.source / 'jquants/latest.json').write_bytes(encoded(report))
        repo = self.root / 'synthetic-repo'
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args],
                                           stderr=subprocess.DEVNULL).decode().strip()
        git('init')
        git('config', 'user.email', 'synthetic@example.test')
        git('config', 'user.name', 'Synthetic')
        git('commit', '--allow-empty', '-m', 'synthetic base')
        base = git('rev-parse', 'HEAD')
        codec.pack(self.source, repo / 'stock.jp/data/jquants-archive')
        git('add', 'stock.jp/data')
        git('commit', '-m', 'synthetic cache')
        proof = daily_publication.validate_local(repo, 'market', '2026-10-02', base,
                                                git('rev-parse', 'HEAD'))
        self.assertEqual(proof['scope'], 'partial_acquisition')
        self.assertFalse(proof['operational_readiness'])

    def test_supported_queries_and_pagination_roundtrip(self):
        queries = [('/equities/bars/daily', {'date': '2026-10-02'}),
                   ('/equities/bars/daily', {'code': '12340', 'from': '2026-09-01', 'to': '2026-10-02'}),
                   ('/fins/earnings-date', {'date': '2026-10-02'}),
                   ('/equities/earnings-calendar', {}),
                   ('/markets/calendar', {'from': '2026-10-02', 'to': '2026-10-05'})]
        for endpoint, params in queries:
            self.client.get(endpoint, params)
            self.client.get(endpoint, {**params, 'pagination_key': 'synthetic-page-2'})
        codec.pack(self.source, self.store)
        self.assertTrue(codec.restore(self.store, self.root / 'restored')['verified'])

    def test_wrong_scope_and_page_hash(self):
        original = self.client.root
        moved = original.with_name('0' * 64)
        original.rename(moved)
        with self.assertRaises(codec.ArchiveError):
            codec.pack(self.source, self.store)
        moved.rename(original)
        self.page.rename(self.page.with_name('0' * 64 + '.json'))
        with self.assertRaises(codec.ArchiveError):
            codec.pack(self.source, self.store)

    def test_rehashed_untrusted_identity_is_rejected(self):
        variants = [('origin', 'https://untrusted.example/v2'), ('plan', 'premium'),
                    ('endpoint', '/equities/bars/daily/am'), ('cutoff', '2026-10-02T21:00:00'),
                    ('params', {'date': '2026-10-02', 'unreviewed': 'query'})]
        for key, value in variants:
            with self.subTest(field=key):
                receipt = copy.deepcopy(self.receipt)
                receipt['identity'][key] = value
                old_scope = self.client.root
                scope_identity = {k: receipt['identity'][k] for k in ('version', 'origin', 'plan', 'cutoff')}
                new_scope = old_scope.with_name(digest(encoded(scope_identity)))
                new_page = self.write_receipt(receipt, rekey=True)
                if new_scope != old_scope:
                    old_scope.rename(new_scope)
                with self.assertRaises(codec.ArchiveError):
                    codec.pack(self.source, self.store)
                if new_scope != old_scope:
                    new_scope.rename(old_scope)
                new_page.unlink()
                self.page.write_bytes(encoded(self.receipt))

    def test_receipt_fields_timestamp_and_reference_validation(self):
        variants = []
        for field, value in [('unexpected', True), ('retrieved_at_jst', '2026-10-02T21:00:00'),
                             ('retrieved_at_jst', '2026-10-02T20:59:59+09:00')]:
            receipt = copy.deepcopy(self.receipt)
            receipt[field] = value
            variants.append(receipt)
        for field, value in [('path', '../outside.json'), ('sha256', '0' * 64),
                             ('bytes', self.receipt['object']['bytes'] + 1), ('unexpected', True)]:
            receipt = copy.deepcopy(self.receipt)
            receipt['object'][field] = value
            variants.append(receipt)
        for index, receipt in enumerate(variants):
            with self.subTest(case=index):
                self.write_receipt(receipt)
                with self.assertRaises(codec.ArchiveError):
                    codec.pack(self.source, self.store)
        self.write_receipt(self.receipt)

    def test_missing_corrupt_and_orphan_objects(self):
        original = self.obj.read_bytes()
        self.obj.unlink()
        with self.assertRaises(codec.ArchiveError):
            codec.pack(self.source, self.store)
        self.obj.write_bytes(b'{"data":[]}')
        with self.assertRaises(codec.ArchiveError):
            codec.pack(self.source, self.store)
        self.obj.write_bytes(original)
        data = encoded({'data': [{'orphan': True}]})
        sha = digest(data)
        orphan = self.client.root / 'objects' / sha[:2] / (sha + '.json')
        orphan.parent.mkdir(exist_ok=True)
        orphan.write_bytes(data)
        with self.assertRaises(codec.ArchiveError):
            codec.pack(self.source, self.store)

    def test_symlink_and_unrecognized_cache_layout(self):
        link = self.client.root / 'pages/link.json'
        link.symlink_to(self.page)
        with self.assertRaises(codec.ArchiveError):
            codec.pack(self.source, self.store)
        link.unlink()
        (self.client.root / 'unexpected.json').write_text('{}')
        with self.assertRaises(codec.ArchiveError):
            codec.pack(self.source, self.store)

    def test_forged_archive_revalidates_provenance_before_restore(self):
        receipt = copy.deepcopy(self.receipt)
        receipt['retrieved_at_jst'] = '2026-10-02T21:00:00'
        self.write_receipt(receipt)
        self.forge_store()
        with self.assertRaises(codec.ArchiveError):
            codec.verify(self.store)
        target = self.root / 'must-not-appear'
        with self.assertRaises(codec.ArchiveError):
            codec.restore(self.store, target)
        self.assertFalse(target.exists())

    def test_valid_hashes_do_not_excuse_invalid_response_schema(self):
        for payload in ({'data': 'wrong'}, {'data': [1]}, {'data': [], 'pagination_key': 1}):
            with self.subTest(payload=payload):
                data = encoded(payload)
                sha = digest(data)
                invalid = self.client.root / 'objects' / sha[:2] / (sha + '.json')
                invalid.parent.mkdir(exist_ok=True)
                invalid.write_bytes(data)
                receipt = copy.deepcopy(self.receipt)
                receipt['object'] = {'path': invalid.relative_to(self.client.root).as_posix(),
                                     'sha256': sha, 'bytes': len(data)}
                original = self.obj.read_bytes()
                self.obj.unlink()
                self.write_receipt(receipt)
                with self.assertRaises(codec.ArchiveError):
                    codec.pack(self.source, self.store)
                invalid.unlink()
                self.obj.write_bytes(original)
                self.write_receipt(self.receipt)

    def test_correctly_hashed_float_overflow_fails_pack_and_restore(self):
        original = self.obj.read_bytes()
        for index, value in enumerate((b'1e400', b'-1e400', b'1.8e308',
                                       b'{"nested":[-1e999]}')):
            with self.subTest(value=value):
                data = b'{"data":[{"Close":' + value + b'}]}\n'
                sha = digest(data)
                invalid = self.client.root / 'objects' / sha[:2] / (sha + '.json')
                invalid.parent.mkdir(exist_ok=True)
                invalid.write_bytes(data)
                receipt = copy.deepcopy(self.receipt)
                receipt['object'] = {'path': invalid.relative_to(self.client.root).as_posix(),
                                     'sha256': sha, 'bytes': len(data)}
                self.obj.unlink()
                self.write_receipt(receipt)
                with self.assertRaises(codec.ArchiveError):
                    codec.pack(self.source, self.store)
                self.forge_store()
                target = self.root / f'overflow-restore-{index}'
                with self.assertRaises(codec.ArchiveError):
                    codec.verify(self.store)
                with self.assertRaises(codec.ArchiveError):
                    codec.restore(self.store, target)
                self.assertFalse(target.exists())
                invalid.unlink()
                self.obj.write_bytes(original)
                self.write_receipt(self.receipt)

    def test_finite_exponents_restore_original_bytes_without_normalization(self):
        data = b'{ "data": [{"values": [1.7e308, -1.7e308, 1e-400, -0.0, 1.25]}] }\n'
        sha = digest(data)
        obj = self.client.root / 'objects' / sha[:2] / (sha + '.json')
        obj.parent.mkdir(exist_ok=True)
        obj.write_bytes(data)
        receipt = copy.deepcopy(self.receipt)
        receipt['object'] = {'path': obj.relative_to(self.client.root).as_posix(),
                             'sha256': sha, 'bytes': len(data)}
        self.obj.unlink()
        self.write_receipt(receipt)
        codec.pack(self.source, self.store)
        target = self.root / 'finite-restored'
        codec.restore(self.store, target)
        restored = target / obj.relative_to(self.source)
        self.assertEqual(restored.read_bytes(), data)
        self.assertEqual(digest(restored.read_bytes()), sha)


if __name__ == '__main__':
    unittest.main()
