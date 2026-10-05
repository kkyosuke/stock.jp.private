from stock_jp.workspace import project_root
from pathlib import Path
import unittest
import yaml

ROOT = project_root()


class GuardrailOwnershipTest(unittest.TestCase):
    def test_only_nightly_workflow_can_write_and_dispatch_ci(self):
        forbidden = (
            "CLOUDFLARE_API_TOKEN", "AUTOMATION_PAT", "create-pull-request@", "gh pr merge",
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
                    self.assertIn('GITHUB_TOKEN: ${{ github.token }}', content)
                    self.assertEqual(yaml.safe_load(content)['permissions'], {
                        'contents': 'write', 'pull-requests': 'write',
                        'actions': 'write', 'checks': 'read'})
                    self.assertNotIn('STOCK_ACTIONS_', content)
                    self.assertIn('python -m stock_jp automation nightly', content)
                    self.assertIn('STOCK_TARGET_DATE: ${{ inputs.target_date }}', content)
                    self.assertNotIn('--target-date ${{', content)
                    self.assertIn("cron: '0 21 * * *'", content)
                    self.assertIn('timezone: Asia/Tokyo', content)
                else:
                    self.assertTrue(all(value in {'read', 'none'} for value in
                                        yaml.safe_load(content)['permissions'].values()))
                if "actions/checkout@" in content:
                    self.assertIn("persist-credentials: false", content)

    def test_only_acquisition_and_readonly_monitors_have_schedules(self):
        scheduled = {"operation-health.yml", "market-data-manual.yml", "forward-holdout.yml"}
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            if path.name not in scheduled:
                self.assertNotIn("  schedule:", path.read_text())
            else:
                self.assertIn("  schedule:", path.read_text())
        holdout = (ROOT / ".github/workflows/forward-holdout.yml").read_text()
        document = yaml.safe_load(holdout)
        self.assertEqual(document['permissions'], {'contents': 'read'})
        self.assertTrue(all('permissions' not in job for job in document['jobs'].values()))
        self.assertIn("github.ref == 'refs/heads/main'", holdout)
        self.assertIn("cron: '30 22 * * *'", holdout)
        self.assertIn('timezone: Asia/Tokyo', holdout)
        self.assertNotIn('secrets.', holdout)
        self.assertIn('git diff --exit-code -- config operations research data', holdout)

    def test_retired_worker_is_removed(self):
        self.assertFalse((ROOT / 'cloudflare').exists())

    def test_contract_preserves_fail_closed_boundaries(self):
        content = (ROOT / 'docs/archive/operations/assistant-daily-operation.md').read_text()
        for expected in ("PAPER_PROPOSED", "HUMAN_ONLY", "WAIT", "Asia/Tokyo", "21:00", "run token", "remote", "research-results.md"):
            self.assertIn(expected, content)


if __name__ == "__main__":
    unittest.main()
