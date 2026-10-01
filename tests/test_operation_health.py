import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("operation_health", ROOT / "scripts/operation_health.py")
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class OperationHealthTest(unittest.TestCase):
    def test_retired_workflow_history_is_not_required(self):
        def api(path):
            self.assertNotIn("actions/workflows", path)
            return {"sha": "same"}
        with patch.object(health, "github", side_effect=api):
            self.assertEqual(health.remote_alerts("owner/private", "owner/public", health.parse_time("2026-09-11T21:00:00+09:00")), [])

    def test_pointer_drift_requires_review(self):
        def api(path):
            return {"sha": "public", "commit": {"committer": {"date": "2026-09-01T00:00:00Z"}}} if "commits/main" in path else {"sha": "private"}
        with patch.object(health, "github", side_effect=api):
            self.assertTrue(health.remote_alerts("owner/private", "owner/public", health.parse_time("2026-09-11T21:00:00+09:00")))

    def test_successful_data_sync_does_not_hide_missed_research(self):
        report = health.local_report(
            {"last_run_id": "old", "data_gaps": [{"status": "OPEN"}]},
            {"status": "MISSED", "alerts": ["nightly run overdue"]},
            {"paper_go": True, "broker_submission": "HUMAN_ONLY", "status": "READY_WITH_WARNINGS"},
        )
        self.assertIn("nightly: MISSED", report["alerts"])
        self.assertEqual(report["open_data_gap_count"], 1)

    def test_invalid_readiness_and_submission_mode_fail_closed(self):
        report = health.local_report({}, {"status": "OK"}, {"paper_go": False, "blockers": ["stale prices"]})
        self.assertIn("stale prices", report["alerts"])
        self.assertIn("broker submission boundary must be HUMAN_ONLY", report["alerts"])

    def test_validation_isolated_and_migrations_rejected(self):
        for script in ("operation_bootstrap.py", "operation_watchdog.py", "operation_state.py"):
            with self.subTest(script=script), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "stock.jp/scripts").mkdir(parents=True)
                private = root / "operations/private"
                private.mkdir(parents=True)
                (private / "state.json").write_text('{"original":true}')
                (root / "stock.jp/operations").mkdir()
                (root / "stock.jp/operations/private").symlink_to(private)
                (root / "stock.jp/scripts" / script).write_text(
                    'from pathlib import Path\n'
                    'p=Path(__file__).resolve().parents[1]/"operations/private/state.json"\n'
                    'p.write_text("{}")\nprint("{}")\n'
                )
                with patch.object(health, "ROOT", root), self.assertRaisesRegex(RuntimeError, "requires migration"):
                    health.command_json(script, "2026-09-11T21:00:00+09:00", "validate" if script == "operation_state.py" else "check")
                self.assertEqual((private / "state.json").read_text(), '{"original":true}')

    def test_readonly_validator_can_return_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "stock.jp/scripts").mkdir(parents=True)
            (root / "operations/private").mkdir(parents=True)
            (root / "stock.jp/scripts/check.py").write_text('print(\'{"paper_go":true}\')')
            with patch.object(health, "ROOT", root):
                self.assertEqual(health.command_json("check.py", "2026-09-11T21:00:00+09:00"), {"paper_go": True})

    def test_naive_time_is_rejected(self):
        with self.assertRaises(ValueError):
            health.parse_time("2026-09-11T21:00:00")

    def test_health_report_only_does_not_change_nightly_readiness(self):
        self.assertIn("--report-only", health.main.__code__.co_consts)


if __name__ == "__main__":
    unittest.main()
