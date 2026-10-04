from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class GuardrailOwnershipTest(unittest.TestCase):
    def test_workflows_have_no_writer_credentials_or_mutating_actions(self):
        forbidden = (
            "CLOUDFLARE_API_TOKEN", "contents: write",
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
                if path.name in {'daily-collector-disabled.yml','history-once-disabled.yml','manual-date-acquisition.yml'}:
                    if path.name == 'history-once-disabled.yml':
                        self.assertIn("if: ${{ false && github.repository == 'kkyosuke/stock.jp.private' && github.ref == 'refs/heads/main' }}",content)
                    else:
                        self.assertIn("if: ${{ github.event_name == 'workflow_dispatch' && github.repository == 'kkyosuke/stock.jp.private' && github.ref == 'refs/heads/main' && github.actor == 'kkyosuke' && github.triggering_actor == 'kkyosuke' }}",content)
                        self.assertIn('STOCK_ACTIONS_ENABLED: ${{ vars.STOCK_ACTIONS_ENABLED }}',content)
                        self.assertIn('contents: read',content)
                    self.assertIn('AUTOMATION_PAT: ${{ secrets.AUTOMATION_PAT }}',content)
                    self.assertIn('STOCK_ACTIONS_APPROVAL_SHA256: ${{ vars.STOCK_ACTIONS_APPROVAL_SHA256 }}',content)
                    if path.name in {'daily-collector-disabled.yml','manual-date-acquisition.yml'}:
                        driver = 'actions_date_driver.py' if path.name == 'manual-date-acquisition.yml' else 'actions_target_driver.py'
                        self.assertIn(f'python stock.jp/scripts/{driver} --target-date "$STOCK_TARGET_DATE"',content)
                        self.assertIn('STOCK_TARGET_DATE: ${{ inputs.target_date }}',content)
                        self.assertIn('required: true',content)
                        self.assertNotIn('--target-date ${{',content)
                    else:
                        self.assertIn('python stock.jp/scripts/actions_driver.py --mode',content)
                    self.assertNotIn('schedule:',content)
                else:
                    self.assertNotIn("AUTOMATION_PAT", content)
                if "actions/checkout@" in content:
                    self.assertIn("persist-credentials: false", content)

    def test_only_health_has_a_schedule(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            if path.name != "operation-health.yml":
                self.assertNotIn("  schedule:", path.read_text())
        self.assertIn("  schedule:", (ROOT / ".github/workflows/operation-health.yml").read_text())

    def test_retired_scheduler_is_removed(self):
        self.assertFalse((ROOT / 'cloudflare/operations-scheduler').exists())
        self.assertFalse((ROOT / '.github/workflows/deploy-operations-scheduler.yml').exists())
        for path in (ROOT / '.github/workflows').glob('*.yml'):
            self.assertNotIn('cloudflare/operations-scheduler', path.read_text())

    def test_contract_preserves_fail_closed_boundaries(self):
        content = (ROOT / "docs/operations/assistant-daily-operation.md").read_text()
        for expected in ("PAPER_PROPOSED", "HUMAN_ONLY", "WAIT", "Asia/Tokyo", "21:00", "run token", "remote", "research-results.md"):
            self.assertIn(expected, content)


if __name__ == "__main__":
    unittest.main()
