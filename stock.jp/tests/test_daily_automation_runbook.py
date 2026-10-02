import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs/operations/daily-automation-runbook-v0.1.md"


class DailyAutomationRunbookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = RUNBOOK.read_text(encoding="utf-8")

    def test_local_markdown_links_resolve(self) -> None:
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", self.text):
            if "://" in target:
                continue
            resolved = (RUNBOOK.parent / target).resolve()
            self.assertTrue(resolved.exists(), target)

    def test_schedule_and_persistent_context_are_explicit(self) -> None:
        for phrase in (
            "月〜金の21:00",
            "Asia/Tokyo",
            "同じチャット",
            "ローカルプロジェクト",
            "分離worktreeは使わない",
            "$japan-stock-operator",
        ):
            self.assertIn(phrase, self.text)

    def test_brokerage_boundary_and_pretrade_check_are_explicit(self) -> None:
        for phrase in (
            "証券会社への注文送信は行わない",
            "8:45〜8:55",
            "PROPOSED",
            "手入力",
        ):
            self.assertIn(phrase, self.text)

    def test_failed_run_does_not_advance_cutoff(self) -> None:
        self.assertIn("`last_disclosure_cutoff_jst` を進めない", self.text)
        self.assertIn("返された`run_token`を保持", self.text)
        self.assertIn("実行が`locked`なら別実行を開始しない", self.text)

    def test_all_tracked_templates_exist(self) -> None:
        for filename in (
            "daily-run-state-template.json",
            "daily-report-template.md",
            "order-ticket-template.csv",
            "pretrade-check-template.md",
            "run-history-template.csv",
            "run-coverage-template.json",
            "source-watermarks.json",
            "source-config-template.json",
            "provider-health-template.json",
            "research-queue-template.json",
            "work-plan-template.json",
            "research-results-template.md",
            "next-day-actions-template.csv",
            "watchlist.csv",
            "global-risk-template.md",
            "investment-case-input-template.json",
        ):
            self.assertTrue((ROOT / "operations/templates" / filename).is_file())


if __name__ == "__main__":
    unittest.main()
