"""Partition the normal unittest discovery for independent CI runners."""
import os
import unittest


GROUPS = ('standard', 'history', 'integration')
CLASS_GROUPS = {
    'tests.test_jquants_history.HistoryTest': 'history',
    'tests.test_actions_review_regressions.FullTreeScopeTests': 'integration',
}


def cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from cases(test)
        else:
            yield test


def select_group(suite, group):
    if group not in GROUPS:
        raise ValueError(f'unknown CI test group: {group!r}')
    selected = unittest.TestSuite(
        test for test in cases(suite)
        if CLASS_GROUPS.get(test.id().rsplit('.', 1)[0], 'standard') == group
    )
    if not selected.countTestCases():
        raise ValueError(f'empty CI test group: {group}')
    return selected


def load_tests(loader, standard_tests, pattern):
    # Use the same discovery as the local full suite. New tests automatically
    # enter the standard group; keep whole classes together for class fixtures.
    suite = loader.discover('tests', top_level_dir='.')
    if loader.errors:
        raise ImportError('\n'.join(loader.errors))
    return select_group(suite, os.environ['CI_TEST_GROUP'])
