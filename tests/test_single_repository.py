"""Exercise a fresh checkout layout and the preserved import inventory."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SingleRepositoryTest(unittest.TestCase):
    def test_ci_has_one_local_checkout_without_cross_repository_secret(self):
        for file in (ROOT / '.github/workflows').glob('*.yml'):
            text = file.read_text()
            for forbidden in ('repository: kkyosuke/stock.jp', 'submodules:', 'git submodule'):
                self.assertNotIn(forbidden, text)
            if file.name not in {'daily-collector-disabled.yml','history-once-disabled.yml'}:
                self.assertNotIn('AUTOMATION_PAT',text)
            if 'actions/checkout@' in text:
                self.assertEqual(text.count('actions/checkout@'), 1)
                self.assertIn('persist-credentials: false', text)
                self.assertIn('!/stock.jp/data/jquants-archive/', text)
                self.assertIn('!/stock.jp/data/daily-prices/', text)
        self.assertFalse((ROOT / '.gitmodules').exists())
        self.assertFalse((ROOT / 'stock.jp/.git').exists())

    def test_source_inventory_is_complete_in_git_and_data_blobs_are_unchanged(self):
        inventory = json.loads((ROOT / 'docs/migrations/common-source-inventory.json').read_text())
        # Read Git metadata, not licensed rows. Sparse CI need not materialize data.
        output = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '--stage', '-z'])
        entries = {}
        for item in output.split(b'\0'):
            if not item:
                continue
            metadata, path = item.split(b'\t')
            mode, sha, stage = metadata.decode().split()
            self.assertNotEqual(mode, '160000')
            self.assertEqual(stage, '0')
            entries[path.decode()] = (mode, sha)
        self.assertEqual(len(inventory['files']), 691)
        for entry in inventory['files']:
            target = 'stock.jp/' + entry['path']
            self.assertIn(target, entries)
            self.assertEqual(entries[target][0], entry['mode'])
            if entry['path'].startswith('data/') and not entry['path'].endswith('.md'):
                self.assertEqual(entries[target][1], entry['sha'])
        self.assertFalse(any(p.startswith('operations/private/market-data/') for p in entries))

    def test_setup_on_fresh_single_repo_is_idempotent_and_refuses_foreign_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'scripts').mkdir()
            (root / 'stock.jp/operations').mkdir(parents=True)
            shutil.copy2(ROOT / 'scripts/setup-workspace.sh', root / 'scripts')
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            def setup():
                return subprocess.run(['sh', str(root / 'scripts/setup-workspace.sh')], capture_output=True)
            self.assertEqual(setup().returncode, 0)
            link = root / 'stock.jp/operations/private'
            self.assertEqual(link.resolve(), root / 'operations/private')
            self.assertEqual(setup().returncode, 0)
            (root / 'stock.jp/.git').write_text('stale checkout')
            self.assertNotEqual(setup().returncode, 0)
            (root / 'stock.jp/.git').unlink()
            link.unlink()
            link.mkdir()
            (link / 'sentinel').write_text('preserve')
            self.assertNotEqual(setup().returncode, 0)
            self.assertEqual((link / 'sentinel').read_text(), 'preserve')

    def test_retired_commands_fail_without_running_git(self):
        for name in ('update-public-submodule.sh', 'validate-public-auto-update.sh'):
            result = subprocess.run(['sh', str(ROOT / 'scripts' / name)], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b'retired', result.stderr)
