from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.new_operation_log import create_log, render_log


class NewOperationLogTest(unittest.TestCase):
    def test_render_replaces_identity_fields(self) -> None:
        rendered = render_log(
            day="2026-08-31",
            code="1234",
            company="Example Inc.",
            mode="monthly",
        )

        self.assertIn("2026-08-31-1234-monthly", rendered)
        self.assertIn("Example Inc.", rendered)
        self.assertIn("BUY / WATCH / WAIT / KEEP / ADD / REDUCE / SELL", rendered)
        self.assertNotIn("{{", rendered)

    def test_create_refuses_to_overwrite_a_log(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "decision.md"
            created = create_log(
                day="2026-08-31",
                code="1234",
                company="Example Inc.",
                mode="quarterly",
                output=output,
            )

            self.assertEqual(created, output)
            self.assertTrue(output.is_file())
            with self.assertRaises(FileExistsError):
                create_log(
                    day="2026-08-31",
                    code="1234",
                    company="Example Inc.",
                    mode="quarterly",
                    output=output,
                )


if __name__ == "__main__":
    unittest.main()
