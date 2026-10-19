"""CI consumes the single dependency definition and never collects real data."""
from pathlib import Path
import tomllib
import subprocess
import unittest
import yaml
from stock_jp.workspace import project_root
from tests.ci_suite import GROUPS

ROOT = project_root()


class DailyStockPriceWorkflowTest(unittest.TestCase):
    def test_static_and_synthetic_checks_have_read_only_permissions(self):
        text = (ROOT / '.github/workflows/ci.yml').read_text()
        workflow = yaml.safe_load(text)
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        self.assertEqual(set(workflow['jobs']), {'format', 'test', 'test-groups', 'guardrail'})
        for job in workflow['jobs'].values():
            for step in job['steps']:
                if step.get('uses', '').startswith('actions/checkout@'):
                    self.assertFalse(step['with']['persist-credentials'])
        for forbidden in ('secrets.', 'git push', 'pull_request_target:', 'schedule:', 'AUTOMATION_PAT'):
            self.assertNotIn(forbidden, text)
        self.assertIn('python -m unittest tests.ci_suite --durations 25', text)
        self.assertIn('python -m compileall -q src tests', text)
        self.assertIn('python -m stock_jp operation smoke --days 20', text)
        self.assertIn('python -m stock_jp automation validate', text)
        self.assertIn('git diff --exit-code -- config operations data/market/observations', text)
        self.assertIn('broker_orders_submitted', (ROOT / 'src/stock_jp/operation/smoke.py').read_text())

    def test_required_test_check_waits_for_every_group_and_rejects_non_success(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
        groups = workflow['jobs']['test-groups']
        self.assertEqual(groups['strategy']['matrix']['group'], list(GROUPS))
        self.assertFalse(groups['strategy']['fail-fast'])
        self.assertNotIn('continue-on-error', groups)
        gate = workflow['jobs']['test']
        self.assertEqual(gate['needs'], 'test-groups')
        self.assertEqual(gate['if'], '${{ always() }}')
        step, = gate['steps']
        self.assertEqual(step['env']['TEST_RESULT'], '${{ needs.test-groups.result }}')
        for result in ('success', 'failure', 'cancelled', 'skipped', ''):
            with self.subTest(result=result):
                completed = subprocess.run(['sh', '-c', step['run']], env={'TEST_RESULT': result})
                self.assertEqual(completed.returncode == 0, result == 'success')

    def test_python_installation_reads_the_project_definition(self):
        with (ROOT / 'pyproject.toml').open('rb') as source:
            project = tomllib.load(source)['project']
        for name in ('ci.yml', 'market-data-manual.yml', 'operation-health.yml'):
            text = (ROOT / '.github/workflows' / name).read_text()
            self.assertIn('python -m pip install -e', text)
            for dependency in project['dependencies']:
                self.assertNotIn(dependency, text)
        self.assertIn("pip install -e '.[dev]'", (ROOT / 'README.md').read_text())
