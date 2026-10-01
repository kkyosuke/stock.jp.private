from pathlib import Path
import os
import subprocess
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PrivateSubmoduleCheckoutTest(unittest.TestCase):
    def test_missing_credential_fails_without_disclosing_values(self):
        for name in ('format-check.yml', 'operation-health.yml'):
            text = (ROOT / '.github/workflows' / name).read_text()
            step = text.split('      - name: Require approved stock.jp checkout credential\n')[1].split('\n      - name:')[0]
            script = textwrap.dedent(step.split('        run: |\n')[1])
            for value, expected in (('', 1), ('synthetic-readonly-test', 0)):
                result = subprocess.run(['bash', '-c', script], env={'PATH': os.defpath, 'AUTOMATION_PAT': value}, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected)
                self.assertNotIn('synthetic-readonly-test', result.stdout + result.stderr)
            self.assertIn('::error::AUTOMATION_PAT is not configured', script)

    def test_exact_pin_and_scoped_separate_checkouts(self):
        for name in ('format-check.yml', 'operation-health.yml'):
            text = (ROOT / '.github/workflows' / name).read_text()
            self.assertNotIn('submodules: recursive', text)
            self.assertIn('git rev-parse HEAD:stock.jp', text)
            self.assertIn('repository: kkyosuke/stock.jp', text)
            self.assertIn('ref: ${{ steps.stock-jp-pin.outputs.sha }}', text)
            self.assertIn('token: ${{ secrets.AUTOMATION_PAT }}', text)
            self.assertEqual(text.count('persist-credentials: false'), 2)
            self.assertIn('!/data/jquants-archive/', text)
            self.assertNotIn('continue-on-error', text)
            self.assertNotIn('contents: write', text)


if __name__ == '__main__':
    unittest.main()
