"""Real local Git regressions; every promisor transport is file:// in a temp directory."""
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stock_jp.operation.paper_offline import LocalGit


class OfflineGitTest(unittest.TestCase):
    def git(self, root, *args, data=None, check=True):
        return subprocess.run(['git', '-C', str(root), *args], input=data,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check).stdout.strip().decode()

    def server(self, root):
        root.mkdir()
        self.git(root, 'init', '-b', 'main')
        self.git(root, 'config', 'user.name', 'Synthetic test')
        self.git(root, 'config', 'user.email', 'synthetic@example.invalid')
        self.git(root, 'config', 'uploadpack.allowFilter', 'true')
        original = self.commit(root, b'original evidence\n')
        changed = self.commit(root, b'replacement evidence\n')
        return original, changed

    def commit(self, root, raw):
        blob = self.git(root, 'hash-object', '-w', '--stdin', data=raw)
        self.git(root, 'read-tree', '--empty')
        self.git(root, 'update-index', '--add', '--cacheinfo', '100644', blob, 'evidence')
        tree = self.git(root, 'write-tree')
        head = self.git(root, 'commit-tree', tree, '-m', 'Synthetic evidence')
        self.git(root, 'update-ref', 'refs/heads/main', head)
        return head, tree, blob

    def test_exact_pin_reads_original_commit_tree_and_blob_despite_replace_refs(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / 'repo'
            original, changed = self.server(root)
            self.git(root, 'update-ref', 'refs/remotes/origin/pinned', original[0])
            for old, new in zip(original, changed):
                self.git(root, 'replace', old, new)
            refs = self.git(root, 'show-ref')
            self.assertIn(changed[2], self.git(root, 'ls-tree', original[0]))  # Confirm the attack is active.
            local = LocalGit(root)
            local.pin('pinned', original[0])
            self.assertEqual(local.tree(original[0]), {'evidence': original[2]})
            self.assertEqual(local.read(original[2]), b'original evidence\n')
            self.assertEqual(self.git(root, 'show-ref'), refs)

    def test_missing_promisor_blob_tree_or_commit_never_launches_fetch(self):
        for kind in ('blob', 'tree', 'commit'):
            with self.subTest(kind=kind), TemporaryDirectory() as temporary:
                base = Path(temporary)
                server = base / 'server'
                _, current = self.server(server)
                root = base / 'client'
                # No network service: even an unfixed lazy fetch can only reach this synthetic file:// server.
                self.git(base, 'clone', '--filter=' + ('blob:none' if kind == 'blob' else 'tree:0'),
                         '--no-checkout', server.as_uri(), str(root))
                if kind == 'commit':
                    # Leave the remote ref but remove only this disposable clone's object pack.
                    for path in (root / '.git/objects/pack').iterdir():
                        path.unlink()
                missing = current[{'commit': 0, 'tree': 1, 'blob': 2}[kind]]
                absent = subprocess.run(['git', '--no-lazy-fetch', '-C', str(root), 'cat-file', '-e', missing],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertNotEqual(absent.returncode, 0)
                before = {p.relative_to(root): p.read_bytes() for p in (root / '.git/objects').rglob('*') if p.is_file()}
                trace = base / 'trace.jsonl'
                local = LocalGit(root)
                with patch.dict(os.environ, {'GIT_TRACE2_EVENT': str(trace), 'GIT_NO_LAZY_FETCH': '0',
                                             'GIT_ALLOW_PROTOCOL': 'file'}):
                    with self.assertRaisesRegex(ValueError, 'offline Git read failed'):
                        if kind == 'blob':
                            local.read(current[2])
                        elif kind == 'tree':
                            local.tree(current[0])
                        else:
                            local.pin('main', current[0])
                events = [json.loads(line) for line in trace.read_text().splitlines()]
                self.assertFalse([event for event in events if event['event'] == 'child_start'],
                                 'offline reads must not even launch git fetch or a transport child')
                after = {p.relative_to(root): p.read_bytes() for p in (root / '.git/objects').rglob('*') if p.is_file()}
                self.assertEqual(after, before)
