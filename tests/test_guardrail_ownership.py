from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class GuardrailOwnershipTest(unittest.TestCase):
    def test_workflows_have_no_writer_credentials_or_mutating_actions(self):
        forbidden = (
            "AUTOMATION_PAT", "CLOUDFLARE_API_TOKEN", "contents: write",
            "pull-requests: write", "create-pull-request@", "gh pr merge",
            "wrangler deploy", "collect_daily_prices.py", "./scripts/update-public-submodule.sh",
            "nightly_operation.py start", "git push", "workflow_run:",
            "python stock.jp/scripts/operation_state.py validate",
        )
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            with self.subTest(path=path.name):
                content = path.read_text()
                for value in forbidden:
                    self.assertNotIn(value, content)
                if "actions/checkout@" in content:
                    self.assertIn("persist-credentials: false", content)

    def test_only_health_has_a_schedule(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            if path.name != "operation-health.yml":
                self.assertNotIn("  schedule:", path.read_text())
        self.assertIn("  schedule:", (ROOT / ".github/workflows/operation-health.yml").read_text())

    def test_retired_worker_has_no_cron_or_dispatch(self):
        self.assertIn("crons = []", (ROOT / "cloudflare/operations-scheduler/wrangler.toml").read_text())
        content = (ROOT / "cloudflare/operations-scheduler/worker.mjs").read_text()
        self.assertNotIn("/dispatches", content)
        self.assertNotIn("AUTOMATION_PAT", content)

    def test_contract_preserves_fail_closed_boundaries(self):
        content = (ROOT / "docs/operations/assistant-daily-operation.md").read_text()
        for expected in ("PAPER_PROPOSED", "HUMAN_ONLY", "WAIT", "Asia/Tokyo", "21:00", "run token", "remote", "research-results.md"):
            self.assertIn(expected, content)


if __name__ == "__main__":
    unittest.main()
