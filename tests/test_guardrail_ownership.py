from stock_jp.workspace import project_root
from pathlib import Path
import unittest

ROOT = project_root()


class GuardrailOwnershipTest(unittest.TestCase):
    def test_workflows_have_no_writer_credentials_or_mutating_actions(self):
        forbidden = (
            "CLOUDFLARE_API_TOKEN", "create-pull-request@", "gh pr merge",
            "wrangler deploy", "collect_daily_prices.py", "./scripts/update-public-submodule.sh",
            "nightly_operation.py start", "git push", "workflow_run:",
            'python src/stock_jp/operation/state.py validate',
        )
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            with self.subTest(path=path.name):
                content = path.read_text()
                for value in forbidden:
                    self.assertNotIn(value, content)
                if path.name == 'market-data-manual.yml':
                    self.assertIn("github.actor == 'kkyosuke' && github.triggering_actor == 'kkyosuke'", content)
                    self.assertIn("github.ref == 'refs/heads/main'", content)
                    self.assertNotIn('AUTOMATION_PAT', content)
                    self.assertIn('GITHUB_TOKEN: ${{ github.token }}', content)
                    for permission in ('contents: write', 'pull-requests: write', 'actions: write', 'checks: read'):
                        self.assertIn(permission, content)
                    self.assertNotIn('STOCK_ACTIONS_', content)
                    self.assertIn('python -m stock_jp automation nightly', content)
                    self.assertIn('STOCK_TARGET_DATE: ${{ inputs.target_date }}', content)
                    self.assertNotIn('--target-date ${{', content)
                    self.assertIn("cron: '0 21 * * *'", content)
                    self.assertIn('timezone: Asia/Tokyo', content)
                else:
                    self.assertNotIn("AUTOMATION_PAT", content)
                    self.assertNotIn('contents: write', content)
                    self.assertNotIn('pull-requests: write', content)
                    self.assertNotIn('actions: write', content)
                if "actions/checkout@" in content:
                    self.assertIn("persist-credentials: false", content)

    def test_only_health_and_market_data_have_schedules(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            if path.name not in {"operation-health.yml", "market-data-manual.yml"}:
                self.assertNotIn("  schedule:", path.read_text())
        self.assertIn("  schedule:", (ROOT / ".github/workflows/operation-health.yml").read_text())

    def test_retired_worker_is_removed(self):
        self.assertFalse((ROOT / 'cloudflare').exists())

    def test_contract_preserves_fail_closed_boundaries(self):
        content = (ROOT / 'docs/archive/operations/assistant-daily-operation.md').read_text()
        for expected in ("PAPER_PROPOSED", "HUMAN_ONLY", "WAIT", "Asia/Tokyo", "21:00", "run token", "remote", "research-results.md"):
            self.assertIn(expected, content)


if __name__ == "__main__":
    unittest.main()
