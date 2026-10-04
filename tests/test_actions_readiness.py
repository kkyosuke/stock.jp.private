"""Setup failures must be actionable without enabling any external I/O."""
from tests.operation_test_support import prepare_write
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import stock_jp.automation.readiness as readiness
import stock_jp.automation.target as driver


class ReadinessTests(unittest.TestCase):
    def test_reports_all_omissions_without_disclosing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = readiness.report(tmp, {'AUTOMATION_PAT': 'private-test-sentinel',
                                          'JQUANTS_API_KEY': 'provider-test-sentinel'})
        self.assertEqual(result['blockers'], ['execution_disabled',
            'reviewed_approval_pin_missing_or_invalid', 'approval_manifest_missing',
            'retirement_receipt_missing'])
        self.assertTrue(all(result['credential_presence'].values()))
        self.assertNotIn('sentinel', json.dumps(result))

    def test_local_presence_never_claims_runtime_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'config'
            directory.mkdir(parents=True)
            prepare_write(directory / 'acquisition.json').write_text('{}')
            prepare_write(Path(tmp) / 'operations/evidence/automation-retirement.json').write_text('{}')
            result = readiness.report(tmp, dict(STOCK_ACTIONS_ENABLED='true',
                STOCK_ACTIONS_APPROVAL_SHA256='a'*64, AUTOMATION_PAT='synthetic',
                JQUANTS_API_KEY='synthetic'))
        self.assertEqual(result['blockers'], [])
        self.assertEqual(result['status'], 'LOCAL_CONFIGURATION_PRESENT')
        self.assertEqual(result['runtime_readiness'], 'NOT_EVALUATED')
        self.assertEqual(result['credential_validity'], 'UNVERIFIED')

    def test_disabled_and_enabled_incomplete_cli_never_construct_transports(self):
        for enabled in ('', 'true'):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as tmp:
                output = io.StringIO()
                with patch.dict(driver.os.environ, {'STOCK_ACTIONS_ENABLED': enabled}, clear=True), \
                     patch('sys.argv', ['driver', '--root', tmp, '--target-date', '2025-08-07']), \
                     patch.object(driver.http, 'GitHubTransport') as github, \
                     patch.object(driver.http, 'ProviderTransport') as provider, \
                     patch.object(driver.Driver, 'execute') as execute, \
                     contextlib.redirect_stdout(output):
                    self.assertEqual(driver.main(), 2)
                github.assert_not_called()
                provider.assert_not_called()
                execute.assert_not_called()
                result = json.loads(output.getvalue())
                self.assertIn('approval_manifest_missing', result['blockers'])
                self.assertFalse(any(result['credential_presence'].values()))


if __name__ == '__main__':
    unittest.main()
