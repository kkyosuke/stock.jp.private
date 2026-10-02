import csv
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from scripts.assessment_pipeline import run_assessments
from scripts.operation_state import PROJECT_ROOT, initialize_or_migrate_workspace
from tests.operation_test_support import write_price_archive
from tests.test_investment_case import complete_case


class AssessmentPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "operations").mkdir()
        shutil.copytree(
            PROJECT_ROOT / "operations/templates", self.root / "operations/templates"
        )
        initialize_or_migrate_workspace(self.root)
        write_price_archive(self.root, ["1234"], price_date="2026-08-31")
        private = self.root / "operations/private"
        with (private / "portfolio-register.csv").open(
            encoding="utf-8", newline=""
        ) as source:
            fields = next(csv.reader(source))
        row = dict.fromkeys(fields, "")
        row.update({"code": "1234", "company": "Example", "status": "OPEN"})
        with (private / "portfolio-register.csv").open(
            "a", encoding="utf-8", newline=""
        ) as destination:
            csv.DictWriter(destination, fieldnames=fields).writerow(row)
        case_path = private / "research-inputs/investment-cases/1234.json"
        case_path.write_text(json.dumps(complete_case()), encoding="utf-8")
        with (private / "market-regime-log.csv").open(
            "a", encoding="utf-8", newline=""
        ) as destination:
            csv.writer(destination).writerow(
                [
                    "2026-08-31",
                    "MRS-v0.1",
                    100,
                    90,
                    1,
                    100,
                    90,
                    1,
                    60,
                    1,
                    20,
                    30,
                    1,
                    101,
                    100,
                    1,
                    5,
                    "NORMAL",
                    1,
                    "https://example.invalid",
                    "2026-09-01T18:30:00+09:00",
                ]
            )
        (private / "runs/2026-09-01").mkdir(parents=True)
        with (private / "runs/2026-09-01/sources.csv").open(
            "w", encoding="utf-8", newline=""
        ) as destination:
            csv.writer(destination).writerow(
                [
                    "source_id",
                    "category",
                    "code",
                    "title",
                    "published_at_jst",
                    "retrieved_at_jst",
                    "url",
                    "primary_source",
                    "used_for_decision",
                    "notes",
                ]
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_existing_mrs_and_private_input_produce_entry_ready_assessment(
        self,
    ) -> None:
        result = run_assessments(
            run_id="2026-09-01",
            at="2026-09-01T18:45:00+09:00",
            fixture_dir=None,
            root=self.root,
        )

        self.assertEqual(result["market_regime_state"], "NORMAL")
        self.assertEqual(result["entry_ready_codes"], ["1234"])
        status = json.loads(
            (self.root / result["assessment_status"]).read_text(encoding="utf-8")
        )
        self.assertEqual(status["companies"][0]["status"], "PASS")
        self.assertTrue(
            (
                self.root
                / "operations/private/runs/2026-09-01/investment-cases/1234.json"
            ).is_file()
        )

    def test_new_disclosure_marks_old_case_stale(self) -> None:
        path = self.root / "operations/private/runs/2026-09-01/sources.csv"
        with path.open("a", encoding="utf-8", newline="") as destination:
            csv.writer(destination).writerow(
                [
                    "new-quarter",
                    "tdnet",
                    "1234",
                    "四半期決算短信",
                    "2026-09-01T15:00:00+09:00",
                    "2026-09-01T18:30:00+09:00",
                    "https://example.invalid/new",
                    "true",
                    "true",
                    "",
                ]
            )

        result = run_assessments(
            run_id="2026-09-01",
            at="2026-09-01T18:45:00+09:00",
            fixture_dir=None,
            root=self.root,
        )

        self.assertEqual(result["entry_ready_codes"], [])
        status = json.loads(
            (self.root / result["assessment_status"]).read_text(encoding="utf-8")
        )
        self.assertEqual(status["companies"][0]["status"], "STALE")


if __name__ == "__main__":
    unittest.main()
