"""CI consumes the single dependency definition and never collects real data."""
from pathlib import Path
import tomllib
import unittest
import yaml
from stock_jp.workspace import project_root

ROOT = project_root()


class DailyStockPriceWorkflowTest(unittest.TestCase):
    def test_static_and_synthetic_checks_have_read_only_permissions(self):
        text = (ROOT / '.github/workflows/ci.yml').read_text()
        workflow = yaml.safe_load(text)
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        self.assertEqual(set(workflow['jobs']), {'format', 'test', 'guardrail'})
        for job in workflow['jobs'].values():
            for step in job['steps']:
                if step.get('uses', '').startswith('actions/checkout@'):
                    self.assertFalse(step['with']['persist-credentials'])
        for forbidden in ('secrets.', 'git push', 'pull_request_target:', 'schedule:', 'AUTOMATION_PAT'):
            self.assertNotIn(forbidden, text)
        self.assertIn('python -m unittest discover -s tests -t . --durations 25', text)
        self.assertIn('python -m compileall -q src tests', text)
        self.assertIn('python -m stock_jp operation smoke --days 20', text)
        self.assertIn('python -m stock_jp automation validate', text)
        self.assertIn('git diff --exit-code -- config operations data/market/observations', text)
        self.assertIn('broker_orders_submitted', (ROOT / 'src/stock_jp/operation/smoke.py').read_text())

    def test_data_validation_replaces_only_code_tests_and_keeps_required_check(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
        job = workflow['jobs']['test']
        self.assertNotIn('if', job)
        self.assertNotIn('strategy', job)
        self.assertNotIn('continue-on-error', job)
        steps = job['steps']
        selection = next(step for step in steps if step.get('id') == 'scope')
        self.assertEqual(selection['env']['CI_BASE_SHA'],
                         '${{ github.event.pull_request.base.sha || github.event.before }}')
        for step in steps:
            self.assertNotIn('continue-on-error', step)
            command = step.get('run', '')
            if 'unittest ' in command or 'compileall ' in command or 'operation smoke' in command:
                self.assertEqual(step['if'], "steps.scope.outputs.mode != 'data'")
            if 'ci validate' in command:
                self.assertEqual(step['if'], "steps.scope.outputs.mode == 'data'")
                self.assertEqual(step['env'], selection['env'])
        checkouts = [step for step in steps if step.get('uses', '').startswith('actions/checkout@')]
        self.assertEqual(checkouts[0]['with']['fetch-depth'], 0)
        self.assertIn('!/data/market/jquants/', checkouts[0]['with']['sparse-checkout'])
        self.assertEqual(checkouts[1]['if'], "steps.scope.outputs.mode == 'data'")
        self.assertNotIn('!/data/market/jquants/', checkouts[1]['with']['sparse-checkout'])

    def test_python_installation_reads_the_project_definition(self):
        with (ROOT / 'pyproject.toml').open('rb') as source:
            project = tomllib.load(source)['project']
        for name in ('ci.yml', 'market-data-manual.yml', 'operation-health.yml'):
            text = (ROOT / '.github/workflows' / name).read_text()
            self.assertIn('python -m pip install -e', text)
            for dependency in project['dependencies']:
                self.assertNotIn(dependency, text)
        self.assertIn("pip install -e '.[dev]'", (ROOT / 'README.md').read_text())
