"""Affected-test selection must widen whenever dependencies cannot be proved."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stock_jp.automation.git import git
from tests import ci_selection as c


class DependencySelectionTest(unittest.TestCase):
    def setUp(self):
        self.files = {
            'src/stock_jp/__init__.py': '',
            'src/stock_jp/domain/__init__.py': '',
            'src/stock_jp/domain/leaf.py': 'value = 1',
            'src/stock_jp/domain/service.py': 'from . import leaf\nvalue = leaf.value',
            'src/stock_jp/unrelated.py': 'value = 2',
            'tests/__init__.py': '',
            'tests/support.py': 'from stock_jp.domain import service',
            'tests/test_service.py': 'from tests import support',
            'tests/test_other.py': 'import stock_jp.unrelated',
        }
        self.modules = {'tests.test_service', 'tests.test_other'}

    def selected(self, paths, *, before=None, after=None, modules=None):
        return c.affected_modules(self.files if before is None else before,
                                  self.files if after is None else after,
                                  paths, self.modules if modules is None else modules)[0]

    def test_transitive_relative_imports_and_test_helpers_select_consumers(self):
        self.assertEqual(self.selected(['src/stock_jp/domain/leaf.py']),
                         {'tests.test_service'} | c.BASELINE)
        self.assertEqual(self.selected(['tests/support.py']), {'tests.test_service'} | c.BASELINE)
        self.assertNotIn('tests.test_other', self.selected(['src/stock_jp/domain/leaf.py']))

    def test_removed_imports_and_deleted_modules_keep_old_dependents(self):
        after = dict(self.files)
        after['src/stock_jp/domain/service.py'] = 'value = 3'
        del after['src/stock_jp/domain/leaf.py']
        self.assertEqual(self.selected(['src/stock_jp/domain/leaf.py', 'src/stock_jp/domain/service.py'], after=after),
                         {'tests.test_service'} | c.BASELINE)

    def test_renamed_source_and_new_tests_use_both_trees(self):
        after = dict(self.files)
        del after['src/stock_jp/domain/leaf.py']
        after['src/stock_jp/domain/renamed.py'] = 'value = 3'
        after['tests/test_new.py'] = 'from stock_jp.domain import renamed'
        selected = self.selected(['src/stock_jp/domain/leaf.py', 'src/stock_jp/domain/renamed.py', 'tests/test_new.py'],
                                 after=after, modules=self.modules | {'tests.test_new'})
        self.assertEqual(selected, {'tests.test_service', 'tests.test_new'} | c.BASELINE)

    def test_star_imports_module_strings_and_file_strings_are_dependencies(self):
        for code in ('from stock_jp.domain import *',
                     'target = "stock_jp.domain.leaf.value"',
                     'file = "src/stock_jp/domain/leaf.py"',
                     'command = "domain.leaf"'):
            after = {**self.files, 'tests/test_other.py': code}
            with self.subTest(code=code):
                self.assertIn('tests.test_other', self.selected(['src/stock_jp/domain/leaf.py'], after=after))

    def test_runtime_loader_consumers_run_for_all_source_changes(self):
        after = {**self.files,
                 'src/stock_jp/dynamic.py': 'import importlib\nload = lambda name: importlib.import_module(name)',
                 'tests/test_dynamic.py': 'from stock_jp import dynamic'}
        selected = self.selected(['src/stock_jp/unrelated.py'], before=after, after=after,
                                 modules=self.modules | {'tests.test_dynamic'})
        self.assertIn('tests.test_dynamic', selected)

    def test_shared_unknown_and_non_python_changes_run_all_ordinary_tests(self):
        for path in ('pyproject.toml', '.github/workflows/ci.yml', 'tests/fixtures/input.json',
                     'config/policy.json', 'operations/state.json', 'docs/rules/frozen.md',
                     'src/stock_jp/domain/__init__.py', 'tests/ci_suite.py', 'unknown.py'):
            with self.subTest(path=path):
                self.assertIsNone(self.selected(['src/stock_jp/domain/leaf.py', path]))

    def test_uncovered_source_and_deleted_tests_run_all(self):
        after = {**self.files, 'src/stock_jp/new.py': 'value = 1'}
        self.assertIsNone(self.selected(['src/stock_jp/new.py'], after=after))
        after = dict(self.files)
        del after['tests/test_other.py']
        self.assertIsNone(self.selected(['tests/test_other.py'], after=after, modules={'tests.test_service'}))

    def test_ordinary_docs_in_mixed_change_do_not_force_unrelated_tests(self):
        self.assertEqual(self.selected(['README.md', 'docs/operations/runbook.md', 'src/stock_jp/domain/leaf.py']),
                         {'tests.test_service'} | c.BASELINE)
        self.assertIsNone(self.selected([]))


class GitSelectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        git(self.root, 'init', '-q')
        helper = DependencySelectionTest()
        helper.setUp()
        for name, content in helper.files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.modules = helper.modules
        self.base = self.commit()
        (self.root / 'src/stock_jp/domain/leaf.py').write_text('value = 2')
        self.head = self.commit()

    def commit(self):
        git(self.root, 'add', '.')
        git(self.root, '-c', 'user.name=CI fixture', '-c', 'user.email=ci@example.test',
            'commit', '-qm', 'synthetic')
        return git(self.root, 'rev-parse', 'HEAD').decode().strip()

    def plan(self, base=None, head=None):
        return c.plan(self.root, self.base if base is None else base,
                      self.head if head is None else head, self.modules)

    def test_committed_diff_selects_transitive_consumers(self):
        selected, reason = self.plan()
        self.assertEqual(selected, {'tests.test_service'} | c.BASELINE)
        self.assertIn('base and head', reason)

    def test_unknown_nonancestor_and_empty_bases_fall_back(self):
        for base in ('', 'HEAD~1', '0' * 40, 'a' * 40, self.head):
            with self.subTest(base=base):
                self.assertIsNone(self.plan(base=base)[0])
        self.assertIsNone(self.plan(base=self.head, head=self.base)[0])

    def test_dirty_and_untracked_checkout_cannot_use_committed_selection(self):
        target = self.root / 'src/stock_jp/unrelated.py'
        target.write_text('value = 5')
        self.assertIsNone(self.plan()[0])
        git(self.root, 'checkout', '--', str(target))
        (self.root / 'tests/test_new.py').write_text('pass')
        self.assertIsNone(self.plan()[0])

    def test_syntax_errors_symlinks_and_excluded_archive_members_fall_back(self):
        target = self.root / 'src/stock_jp/unrelated.py'
        target.write_text('def broken(')
        self.head = self.commit()
        self.assertIsNone(self.plan()[0])
        target.unlink()
        target.symlink_to('domain/leaf.py')
        self.head = self.commit()
        self.assertIsNone(self.plan()[0])
        target.unlink()
        target.write_text('value = 2')
        (self.root / '.gitattributes').write_text('src/stock_jp/domain/leaf.py export-ignore\n')
        self.head = self.commit()
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            c.snapshot(self.root, self.head)

    def test_snapshot_never_extracts_files(self):
        with patch.object(c.tarfile.TarFile, 'extractall', side_effect=AssertionError('unsafe extraction')):
            self.assertIn('src/stock_jp/domain/leaf.py', c.snapshot(self.root, self.head))
