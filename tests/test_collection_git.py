"""Batched collection persistence must publish exactly the original bytes."""
import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stock_jp.automation import nightly as n
from tests.test_nightly_market_data import Repository


class CollectionGitTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Repository(Path(temporary.name))
        self.git = n.Git(self.repo)
        self.parent = self.repo.refs['main']

    def write(self, files, *, parent=None, exists=False):
        return self.git.write(n.CHECKPOINT, parent or self.parent, files, exists=exists)

    def assert_files(self, commit, files):
        tree = self.repo.tree(commit)
        for name, raw in files.items():
            self.assertEqual(tree[name], n.git_blob(raw))
            value = self.repo.objects[tree[name]]
            self.assertEqual(base64.b64decode(value['content']), raw)

    def test_text_batch_keeps_unicode_newlines_and_binary_bytes(self):
        files = {n.RAW_PREFIX + 'jquants/sample.json': '{"会社":"テスト","text":"\\n\\\""}\r\n'.encode(),
                 n.RAW_PREFIX + 'daily-prices/sample.csv': b'code,close\n1234,12.3\n',
                 n.RAW_PREFIX + 'jquants/sample.bin': b'\xff\x00\xfe',
                 n.JOURNAL: b'{}\n'}
        head = self.write(files)
        self.assert_files(head, files)
        writes = [(path, body) for method, path, body in self.repo.calls if method != 'GET']
        self.assertEqual(sum(path.endswith('/git/blobs') for path, _ in writes), 1)
        self.assertEqual(sum(path.endswith('/git/trees') for path, _ in writes), 1)

    def test_large_and_escaped_text_stay_within_serialized_batch_limit(self):
        files = {n.RAW_PREFIX + f'jquants/sample-{i}.json': ('会社' * 25 + str(i)).encode()
                 for i in range(8)}
        files[n.RAW_PREFIX + 'jquants/large.json'] = b'x' * 3000
        files[n.JOURNAL] = b'{}\n'
        with patch.object(n, 'MAX_TREE_BODY', 1024):
            head = self.write(files)
        self.assert_files(head, files)
        batches = [body for method, path, body in self.repo.calls
                   if method == 'POST' and path.endswith('/git/trees')]
        self.assertGreater(len(batches), 1)
        self.assertTrue(all(len(json.dumps(body, separators=(',', ':')).encode()) <= 1024 for body in batches))
        self.assertEqual(sum(method == 'POST' and path.endswith('/git/blobs')
                             for method, path, _ in self.repo.calls), 1)
        self.assertEqual(sum(path.endswith('/git/commits') for _, path, _ in self.repo.calls), 1)
        # Intermediate trees never advance the durable checkpoint.
        self.assertEqual(sum(path.endswith('/git/refs') for _, path, _ in self.repo.calls), 1)

    def test_existing_blobs_are_referenced_and_branch_reads_stay_fresh(self):
        files = {n.JOURNAL: b'{"version":1}\n'}
        head = self.write(files)
        self.repo.calls.clear()
        next_head = self.write(files, parent=head, exists=True)
        self.assert_files(next_head, files)
        batches = [body for method, path, body in self.repo.calls if path.endswith('/git/trees')]
        self.assertTrue(all('sha' in entry and 'content' not in entry for body in batches for entry in body['tree']))
        self.assertFalse(any('/git/commits/' in path for _, path, _ in self.repo.calls))
        self.repo.refs[n.CHECKPOINT] = self.parent
        with self.assertRaisesRegex(n.RemoteStop, 'branch moved'):
            self.write(files, parent=next_head, exists=True)

    def test_bad_tree_identity_mode_or_truncation_never_publishes(self):
        for changed in ({'sha': 'f' * 40}, {'mode': '120000'}, {'type': 'commit'}, None):
            with self.subTest(changed=changed):
                def request(method, path, body=None):
                    result = self.repo(method, path, body)
                    if method == 'GET' and '/git/trees/' in path:
                        if changed is None:
                            result['truncated'] = True
                        else:
                            result['tree'][0].update(changed)
                    return result
                self.git = n.Git(request)
                self.repo.calls.clear()
                with self.assertRaisesRegex(n.RemoteStop, 'uploaded'):
                    self.write({n.JOURNAL: b'{}\n'})
                self.assertNotIn(n.CHECKPOINT, self.repo.refs)
                self.assertNotIn(n.git_blob(b'{}\n'), self.git.blobs)
                self.assertFalse(any(path.endswith(('/git/commits', '/git/refs')) for _, path, _ in self.repo.calls))

    def test_failed_batch_keeps_previous_checkpoint_and_retry_can_resume(self):
        head = self.write({n.JOURNAL: b'{"requests":1}\n'})
        files = {n.RAW_PREFIX + f'jquants/{i}.json': (str(i) * 80).encode() for i in range(8)}
        files[n.JOURNAL] = b'{"requests":2}\n'
        count = 0

        def request(method, path, body=None):
            nonlocal count
            if method == 'POST' and path.endswith('/git/trees'):
                count += 1
                if count == 2:
                    raise n.http.Rejected(503)
            return self.repo(method, path, body)

        self.git = n.Git(request)
        with patch.object(n, 'MAX_TREE_BODY', 512), self.assertRaises(n.http.Rejected):
            self.write(files, parent=head, exists=True)
        self.assertEqual(self.repo.refs[n.CHECKPOINT], head)
        self.git = n.Git(self.repo)
        with patch.object(n, 'MAX_TREE_BODY', 512):
            resumed = self.write(files, parent=head, exists=True)
        self.assert_files(resumed, files)

    def test_invalid_later_file_is_rejected_before_any_upload(self):
        for name, raw in [('unexpected/path', b'{}'), (n.JOURNAL, b'{"api_key":"synthetic"}')]:
            with self.subTest(name=name):
                self.repo.calls.clear()
                with self.assertRaises((n.RemoteStop, n.storage.ArchiveError)):
                    self.write({n.RAW_PREFIX + 'jquants/good.json': b'{}', name: raw})
                self.assertTrue(all(method == 'GET' for method, _, _ in self.repo.calls))


if __name__ == '__main__':
    unittest.main()
