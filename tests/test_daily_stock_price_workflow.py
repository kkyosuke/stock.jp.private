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
        self.assertEqual(set(workflow['jobs']), {'format'})
        for job in workflow['jobs'].values():
            for step in job['steps']:
                if step.get('uses', '').startswith('actions/checkout@'):
                    self.assertFalse(step['with']['persist-credentials'])
        for forbidden in ('secrets.', 'git push', 'pull_request_target:', 'schedule:', 'AUTOMATION_PAT'):
            self.assertNotIn(forbidden, text)
        self.assertIn('python -m tests.ci_suite --profile "$TEST_PROFILE"', text)
        self.assertIn('python -m compileall -q src tests', text)
        self.assertIn('python -m stock_jp operation smoke --days 20', text)
        self.assertIn('python -m stock_jp automation validate', text)
        self.assertIn('git diff --exit-code -- config operations data/market/observations', text)
        self.assertIn('broker_orders_submitted', (ROOT / 'src/stock_jp/operation/smoke.py').read_text())

    def test_data_validation_replaces_only_code_tests_and_keeps_required_check(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
        job = workflow['jobs']['format']
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
            if 'tests.ci_suite' in command:
                self.assertEqual(step['if'], "steps.scope.outputs.mode != 'data'")
                self.assertEqual(step['env']['TEST_PROFILE'], '${{ steps.scope.outputs.mode }}')
                self.assertEqual(step['env']['CI_BASE_SHA'], selection['env']['CI_BASE_SHA'])
                self.assertIn('--base "$CI_BASE_SHA"', command)
            if any(required in command for required in ('lint:markdown', 'YAML.parse_file', 'automation validate')):
                self.assertNotIn('if', step)
            if 'compileall ' in command or 'operation smoke' in command:
                self.assertEqual(step['if'], "steps.scope.outputs.mode == 'code' || steps.scope.outputs.mode == 'full'")
            if 'ci validate' in command:
                self.assertEqual(step['if'], "steps.scope.outputs.mode == 'data'")
                self.assertEqual(step['env'], selection['env'])
        checkouts = [step for step in steps if step.get('uses', '').startswith('actions/checkout@')]
        self.assertEqual(checkouts[0]['with']['fetch-depth'], 0)
        self.assertIn('!/data/market/jquants/', checkouts[0]['with']['sparse-checkout'])
        self.assertEqual(checkouts[1]['if'], "steps.scope.outputs.mode == 'data'")
        self.assertNotIn('!/data/market/jquants/', checkouts[1]['with']['sparse-checkout'])

    def test_full_suite_is_weekly_and_pr_validation_does_not_repeat_on_main_push(self):
        ci = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
        triggers = ci.get('on', ci.get(True))
        self.assertEqual(set(triggers), {'pull_request', 'workflow_dispatch'})
        self.assertEqual(triggers['pull_request']['branches'], ['main'])
        full = yaml.safe_load((ROOT / '.github/workflows/ci-full.yml').read_text())
        triggers = full.get('on', full.get(True))
        self.assertEqual(set(triggers), {'schedule', 'workflow_dispatch'})
        self.assertEqual(triggers['schedule'], [{'cron': '17 4 * * 0', 'timezone': 'Asia/Tokyo'}])
        self.assertEqual(full['permissions'], {'contents': 'read'})
        self.assertEqual(set(full['jobs']), {'full'})
        job = full['jobs']['full']
        self.assertLessEqual(job['timeout-minutes'], 15)
        self.assertTrue(any(step.get('run') == 'python -m tests.ci_suite --profile full' for step in job['steps']))
        self.assertNotIn('continue-on-error', job)
        self.assertTrue(all('continue-on-error' not in step for step in job['steps']))

    def test_acquisition_and_monitors_allocate_only_once_per_day(self):
        expected = {'market-data-manual.yml': '0 21 * * *',
                    'operation-health.yml': '17 23 * * *',
                    'forward-holdout.yml': '30 22 * * *'}
        for name, cron in expected.items():
            with self.subTest(workflow=name):
                workflow = yaml.safe_load((ROOT / '.github/workflows' / name).read_text())
                triggers = workflow.get('on', workflow.get(True))
                self.assertEqual(set(triggers), {'schedule', 'workflow_dispatch'})
                self.assertEqual(triggers['schedule'], [{'cron': cron, 'timezone': 'Asia/Tokyo'}])
        market = yaml.safe_load((ROOT / '.github/workflows/market-data-manual.yml').read_text())
        self.assertEqual(market['jobs']['nightly-market-data']['timeout-minutes'],
                         "${{ github.event_name == 'schedule' && 20 || 90 }}")
        self.assertEqual(market['jobs']['paper-research']['timeout-minutes'], 15)

    def test_python_installation_reads_the_project_definition(self):
        with (ROOT / 'pyproject.toml').open('rb') as source:
            project = tomllib.load(source)['project']
        for name in ('ci.yml', 'market-data-manual.yml', 'operation-health.yml'):
            text = (ROOT / '.github/workflows' / name).read_text()
            self.assertIn('python -m pip install -e', text)
            for dependency in project['dependencies']:
                self.assertNotIn(dependency, text)
        self.assertIn("pip install -e '.[dev]'", (ROOT / 'README.md').read_text())
