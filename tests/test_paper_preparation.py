from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stock_jp.operation.paper_preparation import JST, prepare


class PaperPreparationTest(unittest.TestCase):
    def root(self, temporary):
        root = Path(temporary)
        p = root / "config/policy.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"operation_mode": "PAPER", "broker_submission": "HUMAN_ONLY"}))
        return root

    def test_live_cannot_use_automatic_research_preparation(self):
        with TemporaryDirectory() as temporary:
            root = self.root(temporary)
            (root / "config/policy.json").write_text(json.dumps({"operation_mode": "LIVE", "broker_submission": "HUMAN_ONLY"}))
            with self.assertRaisesRegex(ValueError, "PAPER/HUMAN_ONLY"):
                prepare(root)

    def test_readiness_blocker_prevents_start(self):
        with TemporaryDirectory() as temporary, patch("stock_jp.operation.paper_preparation.check_readiness", return_value={"paper_go": False}), \
                patch("stock_jp.operation.paper_preparation.start_nightly_run") as start:
            result = prepare(self.root(temporary))
            self.assertEqual(result["status"], "INPUTS_BLOCKED")
            start.assert_not_called()

    def test_preparation_closes_partial_attempt_but_never_approves_manual_work(self):
        started = {"status": "in_progress", "run_token": "private-test-token", "run_id": "2026-10-06"}
        with TemporaryDirectory() as temporary, patch("stock_jp.operation.paper_preparation.check_readiness", return_value={"paper_go": True}), \
                patch("stock_jp.operation.paper_preparation.start_nightly_run", return_value=started), \
                patch("stock_jp.operation.paper_preparation.fail_run", return_value={"status": "failed"}) as close:
            result = prepare(self.root(temporary), at=datetime(2026, 10, 6, tzinfo=JST))
            self.assertEqual(result["status"], "REVIEW_REQUIRED")
            self.assertFalse(result["completed_run"])
            self.assertNotIn("run_token", result["run"])
            self.assertTrue(result["source_cutoff_preserved"])
            self.assertEqual(close.call_args.kwargs["run_token"], "private-test-token")
