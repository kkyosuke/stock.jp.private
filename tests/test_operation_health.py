from datetime import datetime
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("operation_health", ROOT / "scripts/operation_health.py")
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class OperationHealthTest(unittest.TestCase):
    def test_only_generated_data_and_pointer_prs_are_monitored(self):
        now = health.parse_time("2026-09-11T09:00:00+09:00")
        run = {"head_branch": "main", "event": "schedule", "created_at": "2026-09-10T23:00:00Z", "status": "completed", "conclusion": "success"}
        def api(path):
            if "/pulls?" in path:
                return [{"number": 25, "head": {"ref": "automation/operations-scheduling"}, "created_at": "2026-09-01T00:00:00Z"}]
            if "/runs?" in path:
                return {"workflow_runs": [run]}
            if "/actions/workflows/" in path:
                return {"state": "active"}
            return {"sha": "same"}
        with patch.object(health, "github", side_effect=api):
            self.assertEqual(health.remote_alerts("owner/private", "owner/public", now), [])

    def test_pat_expiry_warning_and_unknown_expiry_are_visible(self):
        now = health.parse_time("2026-09-11T09:00:00+09:00")
        self.assertTrue(health.expiry_alerts("2026-09-20 00:00:00 UTC", now, "PAT"))
        self.assertTrue(health.expiry_alerts(None, now, "PAT"))
        self.assertEqual(health.expiry_alerts("2026-10-20 00:00:00 UTC", now, "PAT"), [])

    def test_successful_price_sync_does_not_hide_missed_nightly_or_source_gaps(self):
        report = health.local_report(
            {"last_run_id": "2026-09-04", "data_gaps": [{"status": "OPEN"}]},
            {"status": "MISSED", "alerts": ["nightly run overdue", "1 data gap(s)"]},
            {"paper_go": True, "broker_submission": "HUMAN_ONLY", "status": "READY_WITH_WARNINGS"},
        )
        self.assertIn("nightly: MISSED", report["alerts"])
        self.assertEqual(report["open_data_gap_count"], 1)

    def test_invalid_readiness_and_submission_mode_fail_closed(self):
        report = health.local_report({}, {"status": "OK"}, {"paper_go": False, "blockers": ["stale prices"]})
        self.assertIn("stale prices", report["alerts"])
        self.assertIn("broker submission boundary must be HUMAN_ONLY", report["alerts"])

    def test_weekend_and_midnight_collection_deadlines(self):
        for at, expected in (
            ("2026-09-11T23:59:00+09:00", "2026-09-10"),
            ("2026-09-12T00:31:00+09:00", "2026-09-11"),
            ("2026-09-14T09:00:00+09:00", "2026-09-11"),
            ("2026-09-15T00:31:00+09:00", "2026-09-14"),
        ):
            with self.subTest(at=at):
                self.assertEqual(health.required_price_cycle(health.parse_time(at)).date().isoformat(), expected)

    def test_branch_run_cannot_hide_missing_main_run(self):
        self.assertTrue(health.workflow_alerts("sync", [{"head_branch": "feature", "event": "workflow_dispatch"}], health.parse_time("2026-09-11T09:00:00+09:00"), 36))

    def test_failed_stale_and_stuck_runs_require_attention(self):
        now = health.parse_time("2026-09-11T09:00:00+09:00")
        base = {"head_branch": "main", "event": "schedule", "created_at": "2026-09-08T22:00:00Z", "status": "completed", "conclusion": "failure"}
        alerts = health.workflow_alerts("sync", [base], now, 36)
        self.assertEqual(len(alerts), 2)
        self.assertTrue(any("3 hours" in a for a in health.workflow_alerts("sync", [{**base, "status": "queued"}], now, 36)))

    def test_healthy_run_is_accepted_and_naive_time_rejected(self):
        now = health.parse_time("2026-09-11T09:00:00+09:00")
        run = {"head_branch": "main", "event": "schedule", "created_at": "2026-09-10T23:00:00Z", "status": "completed", "conclusion": "success"}
        self.assertEqual(health.workflow_alerts("sync", [run], now, 36), [])
        with self.assertRaises(ValueError):
            health.parse_time("2026-09-11T09:00:00")

    def test_report_only_mode_is_available_for_scheduled_monitoring(self):
        # The CLI remains fail-closed by default; the workflow uses report-only so
        # expected attention states do not look like an infrastructure failure.
        self.assertIn("--report-only", health.main.__code__.co_consts)


if __name__ == "__main__":
    unittest.main()
