"""Canonical layout, sparse checkout metadata, and the installed CLI."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from stock_jp.workspace import project_root

ROOT = project_root()


class SingleRepositoryTest(unittest.TestCase):
    def test_one_package_and_explicit_workflow_responsibilities(self):
        workflows = ROOT / '.github/workflows'
        self.assertEqual({p.name for p in workflows.glob('*.yml')},
                         {'ci.yml', 'ci-full.yml', 'market-data-manual.yml', 'operation-health.yml', 'forward-holdout.yml'})
        for file in workflows.glob('*.yml'):
            text = file.read_text()
            for forbidden in ('submodules:', 'git submodule', 'working-directory: stock.jp'):
                self.assertNotIn(forbidden, text)
            if file.name != 'market-data-manual.yml':
                self.assertNotIn('AUTOMATION_PAT', text)
            if file.name in {'market-data-manual.yml', 'forward-holdout.yml'}:
                self.assertNotIn('!/data/market/jquants/', text)
            else:
                self.assertIn('!/data/market/jquants/', text)
            self.assertIn('persist-credentials: false', text)
            self.assertIn('!/data/market/legacy-prices/', text)
            self.assertIn('pip install -e', text)
        for retired in ('stock.jp', 'scripts', 'cloudflare', 'operations/private', '.gitmodules'):
            self.assertFalse((ROOT / retired).exists(), retired)
        self.assertEqual(list(ROOT.glob('**/pyproject.toml'))[:1], [ROOT / 'pyproject.toml'])

    def test_market_blobs_and_approval_bytes_are_preserved(self):
        report = json.loads((ROOT / 'docs/archive/migrations/layout-migration.json').read_text())
        entries = {}
        for item in subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '--stage', '-z']).split(b'\0'):
            if not item:
                continue
            metadata, path = item.split(b'\t')
            mode, sha, stage = metadata.decode().split()
            self.assertNotEqual(mode, '160000')
            self.assertEqual(stage, '0')
            entries[path.decode()] = (mode, sha)
        self.assertEqual(len(report['market_files']), 544)
        for file in report['market_files']:
            if file['path'] == 'data/market/jquants/current.json':
                # The migration preserves historical objects. Acquisition moves
                # this pointer; the codec validates it. CI excludes data blobs.
                self.assertEqual(entries[file['path']][0], '100644')
                continue
            self.assertEqual(entries[file['path']], ('100644', file['git_blob']))
        self.assertFalse(any(p.startswith(('.work/', '.cache/', 'stock.jp/')) for p in entries))
        import hashlib
        for file in report['preserved_approvals']:
            self.assertEqual(hashlib.sha256((ROOT / file['path']).read_bytes()).hexdigest(), file['sha256'])

    def command(self, root, *arguments):
        environment = dict(os.environ, TMPDIR=str(ROOT / '.work/tmp')) if (ROOT / '.work/tmp').exists() else dict(os.environ)
        return subprocess.run([sys.executable, '-m', 'stock_jp', '--root', str(root), *arguments],
                              cwd=root, env=environment, text=True, capture_output=True, timeout=30)

    def test_cli_initialization_uses_selected_root_and_preserves_existing_records(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copytree(ROOT / 'templates', root / 'templates')
            first = self.command(root, 'operation', 'state', 'init')
            self.assertEqual(first.returncode, 0, first.stderr)
            records = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            second = self.command(root, 'operation', 'state', 'init')
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout)['created'], [])
            self.assertEqual(records, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()})
            self.assertTrue((root / 'config/sources.json').is_file())
            self.assertTrue((root / 'operations/ledgers/capital-ledger.csv').is_file())
            self.assertTrue((root / 'data/market/observations/corporate-actions/latest.csv').is_file())
            self.assertFalse((root / 'stock.jp').exists())
            result = self.command(root, 'operation', 'state', 'validate')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['valid'])

    def test_catalog_requires_no_credentials_and_uses_selected_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').mkdir()
            (root / 'config/sources.json').write_text('{"jquants":{"plan":"light","addons":[]}}')
            result = self.command(root, 'data', 'catalog')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / '.work').exists())
            self.assertIn('datasets', json.loads(result.stdout))
