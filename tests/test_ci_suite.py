"""CI must run every discovered case exactly once and fail closed on errors."""
from collections import Counter
import os
import unittest
from unittest.mock import patch

from tests import ci_suite


class CISuiteTests(unittest.TestCase):
    def test_partition_preserves_all_discovered_tests_once_and_class_fixtures(self):
        loader = unittest.TestLoader()
        suite = loader.discover('tests', top_level_dir='.')
        self.assertEqual(loader.errors, [])
        expected = list(ci_suite.cases(suite))
        actual = []
        owners = {}
        for group in ci_suite.GROUPS:
            selected = list(ci_suite.cases(ci_suite.select_group(suite, group)))
            self.assertTrue(selected)
            self.assertEqual(selected, [test for test in expected if test in selected])
            actual.extend(selected)
            for test in selected:
                class_name = test.id().rsplit('.', 1)[0]
                self.assertEqual(owners.setdefault(class_name, group), group)
        self.assertEqual(Counter(test.id() for test in actual), Counter(test.id() for test in expected))
        self.assertEqual(len(actual), len({test.id() for test in actual}))
        for class_name, group in ci_suite.CLASS_GROUPS.items():
            self.assertEqual(owners[class_name], group)
        self.assertEqual(owners['tests.test_ci_suite.CISuiteTests'], 'standard')

    def test_unknown_or_empty_group_fails(self):
        for group in ('unknown', '', *ci_suite.GROUPS):
            with self.subTest(group=group), self.assertRaises(ValueError):
                ci_suite.select_group(unittest.TestSuite(), group)

    def test_import_errors_fail_even_when_the_broken_module_is_in_another_group(self):
        loader = unittest.TestLoader()
        loader.errors.append('synthetic broken import')
        with patch.object(loader, 'discover', return_value=unittest.TestSuite()):
            for group in ci_suite.GROUPS:
                with self.subTest(group=group), patch.dict(os.environ, CI_TEST_GROUP=group):
                    with self.assertRaisesRegex(ImportError, 'synthetic broken import'):
                        ci_suite.load_tests(loader, unittest.TestSuite(), None)
