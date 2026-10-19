"""Run ordinary regressions on PRs; keep maximum-capacity cases in weekly CI."""
from __future__ import annotations

import argparse
import json
import sys
import unittest

# These exercise maximum scale, not unique safety rules. Smaller history,
# reservation, tree-scope and codec regressions still run on every code PR.
# Keep exact IDs so a rename/deletion fails selection instead of hiding a test.
CAPACITY = frozenset({
    "tests.test_actions_review_regressions.FullTreeScopeTests.test_real_complete_280_history_over_300_files_is_authorized",
    "tests.test_jquants_history.HistoryTest.test_maximum_campaign_280_sessions",
    "tests.test_jquants_history.HistoryTest.test_all_600_failure_reservations_survive_batches",
    "tests.test_actions_sharded.ShardedTests.test_280_sessions_4000_bars_real_codec_capacity",
})
DOC_MODULES = frozenset({
    "tests.test_daily_automation_runbook", "tests.test_daily_stock_price_workflow",
    "tests.test_guardrail_ownership", "tests.test_live_operation_playbook",
    "tests.test_nightly_operation_doc", "tests.test_operation_governance",
    "tests.test_operation_policy", "tests.test_operation_templates",
    "tests.test_public_price_guardrail", "tests.test_single_repository",
})


def flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flatten(test)
        else:
            yield test


def select(tests, profile):
    tests = list(tests)
    ids = [test.id() for test in tests]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate discovered test IDs")
    missing = CAPACITY - set(ids)
    if missing:
        raise ValueError("capacity test inventory changed: " + ", ".join(sorted(missing)))
    if profile not in {"code", "docs", "full"}:
        raise ValueError("unknown CI test profile")
    selected = []
    for test in tests:
        # Discovery errors must fail every profile, including docs-only PRs.
        failed_import = isinstance(test, unittest.loader._FailedTest)
        module = test.__class__.__module__
        if failed_import or profile == "full" or (profile == "code" and test.id() not in CAPACITY) \
                or (profile == "docs" and module in DOC_MODULES):
            selected.append(test)
    if not selected:
        raise ValueError("empty CI test selection")
    if profile == "docs":
        missing_docs = DOC_MODULES - {test.__class__.__module__ for test in tests}
        if missing_docs:
            raise ValueError("documentation test inventory changed")
    return unittest.TestSuite(selected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=("code", "docs", "full"))
    args = parser.parse_args()
    loader = unittest.TestLoader()
    tests = list(flatten(loader.discover("tests", top_level_dir=".")))
    suite = select(tests, args.profile)
    print(json.dumps({"profile": args.profile, "discovered": len(tests),
                      "selected": suite.countTestCases(),
                      "weekly_capacity_cases": len(CAPACITY) if args.profile == "code" else 0}), flush=True)
    timing = {"durations": 25} if sys.version_info >= (3, 12) else {}
    result = unittest.TextTestRunner(verbosity=1, **timing).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
