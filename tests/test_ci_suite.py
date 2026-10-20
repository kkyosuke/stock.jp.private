"""Test discovery must preserve every ordinary test and every failure signal."""
import io
import unittest
from unittest.mock import patch

from tests import ci_suite


class CISuiteTest(unittest.TestCase):
    def test_actual_inventory_defers_exactly_four_cases_and_full_keeps_everything(self):
        tests = list(ci_suite.flatten(unittest.TestLoader().discover('tests', top_level_dir='.')))
        all_ids = {test.id() for test in tests}
        regular = ci_suite.select(tests, 'code')
        full = ci_suite.select(tests, 'full')
        self.assertEqual({test.id() for test in regular}, all_ids - ci_suite.CAPACITY)
        self.assertEqual({test.id() for test in full}, all_ids)
        self.assertEqual(len(ci_suite.CAPACITY), 4)
        self.assertTrue(all_ids - ci_suite.CAPACITY)
        for test in ci_suite.select(tests, 'docs'):
            self.assertIn(test.__class__.__module__, ci_suite.DOC_MODULES)

    def test_new_ordinary_tests_are_included_without_editing_a_manifest(self):
        case = unittest.FunctionTestCase(lambda: None, description='new ordinary test')
        with patch.object(ci_suite, 'CAPACITY', frozenset()):
            self.assertEqual(list(ci_suite.select([case], 'code')), [case])

    def test_duplicate_missing_and_unknown_inventories_fail(self):
        case = unittest.FunctionTestCase(lambda: None)
        with self.assertRaisesRegex(ValueError, 'capacity test inventory'):
            ci_suite.select([case], 'code')
        with patch.object(ci_suite, 'CAPACITY', frozenset()):
            for tests, profile, message in (([case, case], 'code', 'duplicate'),
                                             ([case], 'typo', 'unknown'),
                                             ([], 'code', 'empty')):
                with self.subTest(profile=profile, message=message), self.assertRaisesRegex(ValueError, message):
                    ci_suite.select(tests, profile)

    def test_discovery_errors_fail_even_documentation_profile(self):
        tests = list(ci_suite.flatten(unittest.TestLoader().loadTestsFromName('tests.synthetic_missing_module')))
        with patch.object(ci_suite, 'CAPACITY', frozenset()), patch.object(ci_suite, 'DOC_MODULES', frozenset()):
            for profile in ('code', 'docs', 'full'):
                with self.subTest(profile=profile):
                    suite = ci_suite.select(tests, profile)
                    result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
                    self.assertFalse(result.wasSuccessful())
                    self.assertEqual(len(result.errors), 1)

    def test_dependency_selection_keeps_baseline_and_discovery_failures(self):
        tests = list(ci_suite.flatten(unittest.TestLoader().discover('tests', top_level_dir='.')))
        errors = list(ci_suite.flatten(unittest.TestLoader().loadTestsFromName('tests.synthetic_missing_module')))
        modules = ci_suite.ci_selection.BASELINE | {'tests.test_nightly_market_data'}
        selected = list(ci_suite.select(tests + errors, 'code', modules))
        self.assertTrue(set(errors) <= set(selected))
        self.assertEqual({t.__class__.__module__ for t in selected} - {'unittest.loader'}, modules)
        self.assertTrue(all(t.id() not in ci_suite.CAPACITY for t in selected))
        with self.assertRaisesRegex(ValueError, 'baseline test inventory'):
            ci_suite.select(tests, 'code', {'tests.test_nightly_market_data'})
        # A full run ignores any narrowed selection.
        self.assertEqual(ci_suite.select(tests, 'full', modules).countTestCases(), len(tests))

    def test_regular_test_failure_is_not_converted_to_a_skip_or_success(self):
        def fail():
            raise AssertionError('synthetic regression')
        case = unittest.FunctionTestCase(fail)
        with patch.object(ci_suite, 'CAPACITY', frozenset()):
            result = unittest.TextTestRunner(stream=io.StringIO()).run(ci_suite.select([case], 'code'))
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.skipped, [])
