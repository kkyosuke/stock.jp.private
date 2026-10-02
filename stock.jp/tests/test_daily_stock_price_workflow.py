from __future__ import annotations

from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/daily-stock-prices.yml"
OPERATION_TEST_WORKFLOW = ROOT / ".github/workflows/operation-tests.yml"


class DailyStockPriceWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_all_triggers_are_read_only_guardrails(self) -> None:
        import yaml
        workflow = yaml.safe_load(self.text)
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(set(workflow["jobs"]), {"guardrail"})
        steps = workflow["jobs"]["guardrail"]["steps"]
        self.assertFalse(steps[0]["with"]["persist-credentials"])
        self.assertNotIn("permissions", workflow["jobs"]["guardrail"])
        self.assertEqual(
            [step["uses"].split("@")[0] for step in steps if "uses" in step],
            ["actions/checkout", "actions/setup-python"],
        )
        for forbidden in (
            "secrets.", "AUTOMATION_PAT", "collect_daily_prices.py",
            "nightly_operation.py", "official_source_scan.py", "git push",
            "create-pull-request", "gh pr", "--auto", "workflow_run:",
            "pull_request_target:", "curl ", "wget ", "upload-artifact",
        ):
            self.assertNotIn(forbidden, self.text)
        self.assertIn("workflow_dispatch:", self.text)
        self.assertIn("tests.test_public_price_guardrail", self.text)
        self.assertNotIn("schedule:", self.text)
        self.assertNotIn("CHECK_FRESHNESS", self.text)
        self.assertNotIn("--archive data/daily-prices", self.text)
        self.assertIn("python -m unittest discover -s tests -v", self.text)
        self.assertIn("python -m compileall -q scripts tests", self.text)
        self.assertIn("operation_smoke.py --days 20", self.text)
        self.assertIn("git diff --exit-code", self.text)
        self.assertIn("broker_orders_submitted", (ROOT / "scripts/operation_smoke.py").read_text())

    def test_workflow_keeps_github_token_read_only(self) -> None:
        self.assertIn("permissions:\n  contents: read", self.text)
        self.assertNotIn("contents: write", self.text)
        self.assertNotIn("pull-requests: write", self.text)
        self.assertNotIn("actions: write", self.text)
        self.assertNotIn("id-token: write", self.text)

    def test_workflow_installs_every_project_runtime_dependency(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as source:
            project_dependencies = tomllib.load(source)["project"]["dependencies"]

        install_start = self.text.index("- name: Install dependencies")
        collect_start = self.text.index(
            "- name: Validate synthetic archive guardrails",
            install_start,
        )
        install_step = self.text[install_start:collect_start]

        for dependency in project_dependencies:
            with self.subTest(dependency=dependency):
                self.assertIn(dependency, install_step)

    def test_operation_tests_install_every_project_runtime_dependency(self) -> None:
        operation_test_text = OPERATION_TEST_WORKFLOW.read_text(encoding="utf-8")
        with (ROOT / "pyproject.toml").open("rb") as source:
            project_dependencies = tomllib.load(source)["project"]["dependencies"]

        install_start = operation_test_text.index(
            "- name: Install runtime and skill-validator dependencies"
        )
        unit_test_start = operation_test_text.index(
            "- name: Unit tests",
            install_start,
        )
        install_step = operation_test_text[install_start:unit_test_start]

        for dependency in project_dependencies:
            with self.subTest(dependency=dependency):
                self.assertIn(dependency, install_step)

    def test_readme_installs_every_project_runtime_dependency(self) -> None:
        readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
        with (ROOT / "pyproject.toml").open("rb") as source:
            project_dependencies = tomllib.load(source)["project"]["dependencies"]

        for dependency in project_dependencies:
            with self.subTest(dependency=dependency):
                self.assertIn(dependency, readme_text)


if __name__ == "__main__":
    unittest.main()
